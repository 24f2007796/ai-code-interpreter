from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

APP_NAME = "A2A Invoice Action Agent"
VERSION = "1.0.0"
A2A_MEDIA = "application/a2a+json"
INPUT_MEDIA = "application/vnd.ga5.invoice-claim-batch+json"
PROPOSALS_MEDIA = "application/vnd.ga5.invoice-action-proposals+json"
RESULTS_MEDIA = "application/vnd.ga5.invoice-action-results+json"
RECEIPTS_MEDIA = "application/vnd.ga5.invoice-action-receipts+json"
ACTIONS = {
    "settle_invoice", "request_approval", "hold_invoice",
    "reject_duplicate", "open_exception",
}
DB_PATH = os.getenv("DATABASE_PATH", "invoice_agent.db")
AI_BASE_URL = os.getenv("AI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
AI_MODEL = os.getenv("AI_MODEL", "gpt-4o-mini")
AI_API_KEY = os.getenv("AI_API_KEY") or os.getenv("OPENAI_API_KEY", "")
BASE_URL = os.getenv("A2A_BASE_URL", "")
_lock = threading.RLock()

app = FastAPI(title=APP_NAME, version=VERSION)


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def principal_for(token: str) -> str:
    # Never persist or log the raw bearer token.
    return sha256_text(token)


def now_ms() -> int:
    return int(time.time() * 1000)


def _connect() -> sqlite3.Connection:
    parent = os.path.dirname(os.path.abspath(DB_PATH))
    os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@contextmanager
def connection():
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    with connection() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY,
            context_id TEXT NOT NULL,
            principal TEXT NOT NULL,
            batch_id TEXT NOT NULL,
            state TEXT NOT NULL,
            task_json TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_tasks_principal_created
            ON tasks(principal, created_at);
        CREATE TABLE IF NOT EXISTS message_dedup (
            principal TEXT NOT NULL,
            message_id TEXT NOT NULL,
            message_hash TEXT NOT NULL,
            task_id TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            PRIMARY KEY(principal, message_id)
        );
        CREATE TABLE IF NOT EXISTS decision_cache (
            content_hash TEXT PRIMARY KEY,
            decision_json TEXT NOT NULL,
            created_at INTEGER NOT NULL
        );
        """)


@app.on_event("startup")
def _startup() -> None:
    init_db()


@contextmanager
def immediate_transaction():
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


def task_state(task: dict[str, Any]) -> str:
    return task.get("status", {}).get("state", "")


def json_response(data: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(content=data, status_code=status_code, media_type=A2A_MEDIA)


def error(code: str, status_code: int, message: str = "Request could not be processed.") -> JSONResponse:
    return json_response({"error": {"code": code, "message": message}}, status_code)


def task_envelope(task: dict[str, Any]) -> dict[str, Any]:
    return {"task": task}


def require_request_headers(request: Request) -> JSONResponse | None:
    version = request.headers.get("A2A-Version")
    if version != "1.0":
        return error("INVALID_A2A_VERSION", 400, "A2A-Version must be 1.0.")
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if request.method == "POST" and content_type != A2A_MEDIA:
        return error("UNSUPPORTED_MEDIA_TYPE", 415, "Content-Type must be application/a2a+json.")
    return None


def authenticated_principal(request: Request) -> str | JSONResponse:
    header = request.headers.get("authorization", "")
    match = re.fullmatch(r"Bearer ([^\s]+)", header)
    if not match:
        return error("UNAUTHENTICATED", 401, "A Bearer token is required.")
    return principal_for(match.group(1))


def get_task(conn: sqlite3.Connection, task_id: str, principal: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT task_json FROM tasks WHERE task_id=? AND principal=?", (task_id, principal)
    ).fetchone()
    if row is None:
        return None
    return json.loads(row["task_json"])


def save_task(conn: sqlite3.Connection, task: dict[str, Any], principal: str, batch_id: str) -> None:
    timestamp = now_ms()
    state = task_state(task)
    conn.execute(
        """INSERT INTO tasks(task_id, context_id, principal, batch_id, state, task_json, created_at, updated_at)
           VALUES(?,?,?,?,?,?,?,?)
           ON CONFLICT(task_id) DO UPDATE SET state=excluded.state, task_json=excluded.task_json,
           updated_at=excluded.updated_at""",
        (task["id"], task["contextId"], principal, batch_id, state,
         canonical_json(task), timestamp, timestamp),
    )


def record_message(conn: sqlite3.Connection, principal: str, message: dict[str, Any],
                   task_id: str) -> tuple[str, str | None]:
    message_id = message.get("messageId")
    if not isinstance(message_id, str) or not message_id:
        raise ValueError("MESSAGE_ID_REQUIRED")
    message_hash = sha256_text(canonical_json(message))
    row = conn.execute(
        "SELECT message_hash, task_id FROM message_dedup WHERE principal=? AND message_id=?",
        (principal, message_id),
    ).fetchone()
    if row:
        if row["message_hash"] != message_hash:
            raise ConflictError("IDEMPOTENCY_CONFLICT")
        return "REPLAY", row["task_id"]
    conn.execute(
        "INSERT INTO message_dedup(principal,message_id,message_hash,task_id,created_at) VALUES(?,?,?,?,?)",
        (principal, message_id, message_hash, task_id, now_ms()),
    )
    return "NEW", None


class ConflictError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class ProtocolError(Exception):
    def __init__(self, code: str, status_code: int = 400, message: str = "Invalid request."):
        self.code, self.status_code, self.message = code, status_code, message
        super().__init__(code)


def validate_message(message: Any) -> dict[str, Any]:
    if not isinstance(message, dict):
        raise ProtocolError("INVALID_MESSAGE")
    if message.get("role") != "ROLE_USER":
        raise ProtocolError("INVALID_MESSAGE_ROLE")
    if not isinstance(message.get("messageId"), str) or not message["messageId"]:
        raise ProtocolError("MESSAGE_ID_REQUIRED")
    if not isinstance(message.get("parts"), list) or not message["parts"]:
        raise ProtocolError("MESSAGE_PART_REQUIRED")
    return message


def find_data_part(message: dict[str, Any], media_type: str) -> dict[str, Any]:
    matches = [p for p in message["parts"] if isinstance(p, dict) and p.get("mediaType") == media_type]
    if len(matches) != 1 or not isinstance(matches[0].get("data"), dict):
        raise ProtocolError("INVALID_MESSAGE_PART")
    return matches[0]["data"]


def _package_id(package: dict[str, Any]) -> str:
    value = package.get("packageId")
    if not isinstance(value, str) or not value.strip():
        raise ProtocolError("PACKAGE_ID_REQUIRED")
    return value


def _cache_content(package: dict[str, Any], policy_revision: str) -> dict[str, Any]:
    # Batch/task/package namespace IDs must not defeat semantic cache reuse.
    value = dict(package)
    value.pop("packageId", None)
    return {"policyRevision": policy_revision, "package": value}


def _extract_json_from_model(text: str) -> Any:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("Model did not return a JSON object")
    return json.loads(text[start:end + 1])


def call_ai_for_decisions(batch_id: str, policy_revision: str,
                          packages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not AI_API_KEY and "localhost" not in AI_BASE_URL and "127.0.0.1" not in AI_BASE_URL:
        raise ProtocolError("AI_NOT_CONFIGURED", 503, "AI provider is not configured.")
    system_prompt = (
        "You are an evidence-based accounts-payable decision engine. Read each supplied invoice package "
        "and return exactly one JSON object: {\"decisions\":[...]} with one decision per package. "
        "Allowed actions: settle_invoice (valid, reconciled, within autonomous authority), "
        "request_approval (commercially valid but outside delegated authority), "
        "hold_invoice (payment pauses pending stated verification), reject_duplicate (same commercial "
        "invoice already paid; a different invoice from the same vendor is NOT a duplicate), "
        "open_exception (material records conflict). Read the current controlling paragraph, not cover "
        "sheets, archive examples, training decoys, stale examples, or irrelevant action words. Respect "
        "negation and policy revision. Use exactly the three decisive bracketed evidence references from "
        "the paragraph that determines the action. Do not include cover-sheet refs or decoys. Do not invent "
        "facts or evidence. Return fields: packageId, action, facts {vendorName, invoiceNumber, amountMinor, "
        "currency}, evidenceRefs (array of exactly 3 strings), rationale (60-1500 characters naming the "
        "action and citing at least 2 of those refs). amountMinor is an integer in the smallest currency unit. "
        "Use INR and paise when applicable. If facts are truly unavailable, use empty strings / 0 only when "
        "the documents support that absence; never guess. Output JSON only."
    )
    payload = {
        "model": AI_MODEL,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": canonical_json({
                "batchId": batch_id, "policyRevision": policy_revision, "packages": packages
            })},
        ],
    }
    req = urllib.request.Request(
        f"{AI_BASE_URL}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            **({"Authorization": f"Bearer {AI_API_KEY}"} if AI_API_KEY else {}),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=32) as resp:
            response = json.loads(resp.read().decode("utf-8"))
        content = response["choices"][0]["message"]["content"]
        parsed = _extract_json_from_model(content)
        decisions = parsed.get("decisions")
        if not isinstance(decisions, list):
            raise ValueError("Missing decisions array")
        return decisions
    except urllib.error.HTTPError as exc:
        raise ProtocolError("AI_PROVIDER_ERROR", 502, "AI provider request failed.") from exc
    except (urllib.error.URLError, TimeoutError, KeyError, IndexError, ValueError, json.JSONDecodeError) as exc:
        raise ProtocolError("AI_RESPONSE_INVALID", 502, "AI provider returned an invalid response.") from exc


def validate_decision(decision: Any, expected_package_id: str) -> dict[str, Any]:
    if not isinstance(decision, dict) or decision.get("packageId") != expected_package_id:
        raise ProtocolError("AI_DECISION_SCHEMA_INVALID", 502, "AI decision did not match the package.")
    action = decision.get("action")
    if action not in ACTIONS:
        raise ProtocolError("AI_DECISION_SCHEMA_INVALID", 502, "AI returned an unsupported action.")
    facts = decision.get("facts")
    if not isinstance(facts, dict):
        raise ProtocolError("AI_DECISION_SCHEMA_INVALID", 502, "AI facts are missing.")
    vendor = facts.get("vendorName")
    invoice = facts.get("invoiceNumber")
    amount = facts.get("amountMinor")
    currency = facts.get("currency")
    if not isinstance(vendor, str) or not isinstance(invoice, str) or not isinstance(currency, str):
        raise ProtocolError("AI_DECISION_SCHEMA_INVALID", 502, "AI facts have invalid types.")
    if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
        raise ProtocolError("AI_DECISION_SCHEMA_INVALID", 502, "AI amountMinor must be a nonnegative integer.")
    refs = decision.get("evidenceRefs")
    if not isinstance(refs, list) or len(refs) != 3 or not all(isinstance(r, str) and r.strip() for r in refs):
        raise ProtocolError("AI_DECISION_SCHEMA_INVALID", 502, "Exactly three evidence references are required.")
    if len(set(refs)) != 3 or not all("[" in r and "]" in r for r in refs):
        raise ProtocolError("AI_DECISION_SCHEMA_INVALID", 502, "Evidence references must be distinct bracketed references.")
    rationale = decision.get("rationale")
    if not isinstance(rationale, str) or not 60 <= len(rationale) <= 1500:
        raise ProtocolError("AI_DECISION_SCHEMA_INVALID", 502, "Rationale must be 60–1500 characters.")
    cited = sum(1 for ref in refs if ref in rationale)
    if cited < 2 or action not in rationale:
        raise ProtocolError("AI_DECISION_SCHEMA_INVALID", 502, "Rationale must name the action and cite at least two references.")
    return {
        "packageId": expected_package_id,
        "action": action,
        "facts": {
            "vendorName": vendor,
            "invoiceNumber": invoice,
            "amountMinor": amount,
            "currency": currency,
        },
        "evidenceRefs": refs,
        "rationale": rationale,
    }


def make_decisions(batch_id: str, policy_revision: str, packages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    init_db()
    cache_keys: list[str] = []
    uncached: list[dict[str, Any]] = []
    package_by_id = {_package_id(p): p for p in packages}
    for package in packages:
        key = sha256_text(canonical_json(_cache_content(package, policy_revision)))
        cache_keys.append(key)
    with connection() as conn:
        cached_rows = conn.execute(
            f"SELECT content_hash, decision_json FROM decision_cache WHERE content_hash IN ({','.join('?' for _ in cache_keys)})",
            cache_keys,
        ).fetchall() if cache_keys else []
    cache = {r["content_hash"]: json.loads(r["decision_json"]) for r in cached_rows}
    for package, key in zip(packages, cache_keys):
        if key not in cache:
            uncached.append(package)

    new_decisions: dict[str, dict[str, Any]] = {}
    if uncached:
        raw = call_ai_for_decisions(batch_id, policy_revision, uncached)
        expected_ids = {_package_id(p) for p in uncached}
        if len(raw) != len(uncached):
            raise ProtocolError("AI_DECISION_COUNT_INVALID", 502, "AI returned the wrong number of decisions.")
        for item in raw:
            if not isinstance(item, dict):
                raise ProtocolError("AI_DECISION_SCHEMA_INVALID", 502, "AI decision is not an object.")
            pid = item.get("packageId")
            if pid not in expected_ids or pid in new_decisions:
                raise ProtocolError("AI_DECISION_SCHEMA_INVALID", 502, "AI returned duplicate or unknown package IDs.")
            new_decisions[pid] = validate_decision(item, pid)
        with immediate_transaction() as conn:
            for package in uncached:
                pid = _package_id(package)
                key = sha256_text(canonical_json(_cache_content(package, policy_revision)))
                conn.execute(
                    "INSERT OR IGNORE INTO decision_cache(content_hash,decision_json,created_at) VALUES(?,?,?)",
                    (key, canonical_json(new_decisions[pid]), now_ms()),
                )
                row = conn.execute("SELECT decision_json FROM decision_cache WHERE content_hash=?", (key,)).fetchone()
                cache[key] = json.loads(row["decision_json"])

    output = []
    for package, key in zip(packages, cache_keys):
        pid = _package_id(package)
        decision = cache[key]
        # Cached decisions are semantic; bind to the current package namespace.
        decision = dict(decision)
        decision["packageId"] = pid
        output.append(validate_decision(decision, pid))
    return output


def make_action_id() -> str:
    return "act_" + uuid.uuid4().hex


def make_artifact(media_type: str, data: dict[str, Any]) -> dict[str, Any]:
    return {
        "artifactId": "artifact_" + uuid.uuid4().hex,
        "parts": [{"mediaType": media_type, "data": data}],
    }


def make_task(task_id: str, context_id: str, message: dict[str, Any],
              proposals: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": task_id,
        "contextId": context_id,
        "status": {"state": "TASK_STATE_INPUT_REQUIRED"},
        "history": [message],
        "artifacts": [make_artifact(PROPOSALS_MEDIA, proposals)],
        "metadata": {},
    }


def validate_batch(data: dict[str, Any]) -> tuple[str, str, list[dict[str, Any]]]:
    batch_id = data.get("batchId")
    policy_revision = data.get("policyRevision")
    packages = data.get("packages")
    if not isinstance(batch_id, str) or not batch_id:
        raise ProtocolError("BATCH_ID_REQUIRED")
    if not isinstance(policy_revision, str) or not policy_revision:
        raise ProtocolError("POLICY_REVISION_REQUIRED")
    if not isinstance(packages, list) or not packages:
        raise ProtocolError("PACKAGES_REQUIRED")
    if not all(isinstance(p, dict) for p in packages):
        raise ProtocolError("INVALID_PACKAGE")
    ids = [_package_id(p) for p in packages]
    if len(set(ids)) != len(ids):
        raise ProtocolError("DUPLICATE_PACKAGE_ID")
    return batch_id, policy_revision, packages


def handle_initial(message: dict[str, Any], principal: str) -> JSONResponse:
    data = find_data_part(message, INPUT_MEDIA)
    batch_id, policy_revision, packages = validate_batch(data)
    message_id = message["messageId"]
    message_hash = sha256_text(canonical_json(message))

    # Serialize initial submissions for the single-worker SQLite deployment.
    # Replays/conflicts are detected before any model call.
    with _lock:
        with connection() as conn:
            existing = conn.execute(
                "SELECT message_hash, task_id FROM message_dedup WHERE principal=? AND message_id=?",
                (principal, message_id),
            ).fetchone()
            if existing:
                if existing["message_hash"] != message_hash:
                    raise ConflictError("IDEMPOTENCY_CONFLICT")
                task = get_task(conn, existing["task_id"], principal)
                if task is None:
                    raise ProtocolError("TASK_NOT_FOUND", 404)
                return json_response(task_envelope(task))

        decisions = make_decisions(batch_id, policy_revision, packages)
        proposals = []
        for decision in decisions:
            proposals.append({
                "packageId": decision["packageId"],
                "actionId": make_action_id(),
                "action": decision["action"],
                "facts": decision["facts"],
                "evidenceRefs": decision["evidenceRefs"],
                "rationale": decision["rationale"],
            })
        proposal_data = {"batchId": batch_id, "proposals": proposals}
        task_id, context_id = "task_" + uuid.uuid4().hex, "ctx_" + uuid.uuid4().hex
        task = make_task(task_id, context_id, message, proposal_data)

        with immediate_transaction() as conn:
            # Recheck inside the write transaction as a defensive guard.
            existing = conn.execute(
                "SELECT message_hash, task_id FROM message_dedup WHERE principal=? AND message_id=?",
                (principal, message_id),
            ).fetchone()
            if existing:
                if existing["message_hash"] != message_hash:
                    raise ConflictError("IDEMPOTENCY_CONFLICT")
                prior = get_task(conn, existing["task_id"], principal)
                if prior is None:
                    raise ProtocolError("TASK_NOT_FOUND", 404)
                return json_response(task_envelope(prior))
            conn.execute(
                "INSERT INTO message_dedup(principal,message_id,message_hash,task_id,created_at) VALUES(?,?,?,?,?)",
                (principal, message_id, message_hash, task_id, now_ms()),
            )
            save_task(conn, task, principal, batch_id)
        return json_response(task_envelope(task))


def handle_results(message: dict[str, Any], principal: str) -> JSONResponse:
    task_id, context_id = message.get("taskId"), message.get("contextId")
    if not isinstance(task_id, str) or not isinstance(context_id, str):
        raise ProtocolError("TASK_CONTEXT_REQUIRED")
    data = find_data_part(message, RESULTS_MEDIA)

    with immediate_transaction() as conn:
        # Reserve/deduplicate the continuation ID before applying the state transition.
        row = conn.execute("SELECT task_id FROM tasks WHERE task_id=? AND principal=?", (task_id, principal)).fetchone()
        if row is None:
            raise ProtocolError("TASK_NOT_FOUND", 404)
        task = get_task(conn, task_id, principal)
        if task is None or task.get("contextId") != context_id:
            raise ProtocolError("TASK_NOT_FOUND", 404)
        existing_status, existing_task_id = record_message(conn, principal, message, task_id)
        if existing_status == "REPLAY":
            replay = get_task(conn, existing_task_id or "", principal)
            if replay is None:
                raise ProtocolError("TASK_NOT_FOUND", 404)
            return json_response(task_envelope(replay))
        if task_state(task) in ("TASK_STATE_COMPLETED", "TASK_STATE_CANCELED", "TASK_STATE_REJECTED", "TASK_STATE_FAILED"):
            # A new continuation cannot mutate a terminal task. Roll back its message-id reservation.
            raise ConflictError("TASK_TERMINAL")
        artifacts = task.get("artifacts", [])
        if len(artifacts) != 1 or artifacts[0].get("parts", [{}])[0].get("mediaType") != PROPOSALS_MEDIA:
            raise ProtocolError("STORED_PROPOSAL_INVALID", 409)
        proposal_data = artifacts[0]["parts"][0].get("data", {})
        if data.get("batchId") != proposal_data.get("batchId"):
            raise ProtocolError("BATCH_MISMATCH")
        if task.get("id") != task_id or task.get("contextId") != context_id:
            raise ProtocolError("TASK_CONTEXT_MISMATCH")
        proposals = proposal_data.get("proposals", [])
        proposal_map = {p["packageId"]: p for p in proposals}
        results = data.get("results")
        if not isinstance(results, list) or len(results) != len(proposals):
            raise ProtocolError("RESULT_SET_INVALID")
        seen = set()
        accepted_executions = []
        for result in results:
            if not isinstance(result, dict):
                raise ProtocolError("RESULT_INVALID")
            pid = result.get("packageId")
            proposal = proposal_map.get(pid)
            if proposal is None or pid in seen:
                raise ProtocolError("RESULT_PACKAGE_MISMATCH")
            seen.add(pid)
            if result.get("actionId") != proposal.get("actionId") or result.get("action") != proposal.get("action"):
                raise ProtocolError("RESULT_PROPOSAL_MISMATCH")
            if result.get("outcome") not in ("ACCEPTED", "REJECTED"):
                raise ProtocolError("RESULT_OUTCOME_INVALID")
            nonce = result.get("receiptNonce")
            if not isinstance(nonce, str) or not nonce:
                raise ProtocolError("RECEIPT_NONCE_REQUIRED")
            if result["outcome"] == "ACCEPTED":
                accepted_executions.append({
                    "packageId": pid,
                    "actionId": proposal["actionId"],
                    "action": proposal["action"],
                    "receiptNonce": nonce,
                    "facts": proposal["facts"],
                    "evidenceRefs": proposal["evidenceRefs"],
                })
        task.setdefault("history", []).append(message)
        task["artifacts"].append(make_artifact(RECEIPTS_MEDIA, {
            "batchId": proposal_data["batchId"],
            "executions": accepted_executions,
        }))
        task["status"] = {"state": "TASK_STATE_COMPLETED"}
        save_task(conn, task, principal, proposal_data["batchId"])
    return json_response(task_envelope(task))


@app.get("/health")
def health():
    return JSONResponse({"status": "ok", "version": VERSION})


@app.get("/.well-known/agent-card.json")
def agent_card(request: Request):
    # Discovery is public and deliberately does not require A2A-Version or Bearer auth.
    origin = str(request.base_url).rstrip("/")
    base = BASE_URL or origin + "/a2a"
    return JSONResponse({
        "name": APP_NAME,
        "description": "Evidence-based invoice action proposals with approval-gated, idempotent receipts.",
        "version": VERSION,
        "capabilities": {"streaming": False, "pushNotifications": False, "stateTransitionHistory": True},
        "supportedInterfaces": [{"url": base, "protocolBinding": "HTTP+JSON", "protocolVersion": "1.0"}],
        "defaultInputModes": [INPUT_MEDIA],
        "defaultOutputModes": [PROPOSALS_MEDIA, RECEIPTS_MEDIA],
        "skills": [{
            "id": "invoice_action_agent",
            "name": "Invoice Action Agent",
            "description": "Classifies invoice packages with evidence and executes only grader-accepted proposals.",
            "tags": ["invoice", "accounts-payable", "evidence", "idempotency", "approval"],
        }],
    })


@app.post("/a2a/message:send")
async def send_message(request: Request):
    header_error = require_request_headers(request)
    if header_error:
        return header_error
    principal = authenticated_principal(request)
    if isinstance(principal, JSONResponse):
        return principal
    try:
        body = await request.json()
        if not isinstance(body, dict) or not isinstance(body.get("message"), dict):
            raise ProtocolError("MESSAGE_REQUIRED")
        message = validate_message(body["message"])
        if any(p.get("mediaType") == INPUT_MEDIA for p in message["parts"] if isinstance(p, dict)):
            return handle_initial(message, principal)
        if any(p.get("mediaType") == RESULTS_MEDIA for p in message["parts"] if isinstance(p, dict)):
            return handle_results(message, principal)
        raise ProtocolError("UNSUPPORTED_MESSAGE_MEDIA_TYPE", 415)
    except ConflictError as exc:
        return error(exc.code, 409)
    except ProtocolError as exc:
        return error(exc.code, exc.status_code, exc.message)
    except (ValueError, json.JSONDecodeError):
        return error("INVALID_REQUEST", 400)


@app.get("/a2a/tasks")
def list_tasks(request: Request):
    header_error = require_request_headers(request)
    if header_error:
        return header_error
    principal = authenticated_principal(request)
    if isinstance(principal, JSONResponse):
        return principal
    with connection() as conn:
        rows = conn.execute(
            "SELECT task_json FROM tasks WHERE principal=? ORDER BY created_at DESC", (principal,)
        ).fetchall()
    return json_response({"tasks": [json.loads(row["task_json"]) for row in rows]})


@app.get("/a2a/tasks/{task_id}")
def read_task(task_id: str, request: Request):
    header_error = require_request_headers(request)
    if header_error:
        return header_error
    principal = authenticated_principal(request)
    if isinstance(principal, JSONResponse):
        return principal
    with connection() as conn:
        task = get_task(conn, task_id, principal)
    if task is None:
        return error("TASK_NOT_FOUND", 404)
    return json_response(task)


@app.post("/a2a/tasks/{task_id}:cancel")
async def cancel_task(task_id: str, request: Request):
    header_error = require_request_headers(request)
    if header_error:
        return header_error
    principal = authenticated_principal(request)
    if isinstance(principal, JSONResponse):
        return principal
    try:
        async with _async_noop_lock():
            with immediate_transaction() as conn:
                task = get_task(conn, task_id, principal)
                if task is None:
                    raise ProtocolError("TASK_NOT_FOUND", 404)
                if task_state(task) in ("TASK_STATE_COMPLETED", "TASK_STATE_CANCELED", "TASK_STATE_REJECTED", "TASK_STATE_FAILED"):
                    raise ConflictError("TASK_TERMINAL")
                task["status"] = {"state": "TASK_STATE_CANCELED"}
                # Keep only the proposal artifact; cancellation must never emit receipts.
                task["artifacts"] = [a for a in task.get("artifacts", [])
                                     if a.get("parts", [{}])[0].get("mediaType") == PROPOSALS_MEDIA]
                save_task(conn, task, principal, task.get("artifacts", [{}])[0].get("parts", [{}])[0].get("data", {}).get("batchId", ""))
        return json_response(task)
    except ConflictError as exc:
        return error(exc.code, 409)
    except ProtocolError as exc:
        return error(exc.code, exc.status_code, exc.message)


class _async_noop_lock:
    async def __aenter__(self):
        _lock.acquire()
        return self
    async def __aexit__(self, exc_type, exc, tb):
        _lock.release()


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    # Never return stack traces, tokens, invoice contents, or other principals' task IDs.
    return error("INTERNAL_ERROR", 500, "Internal server error.")


# Local convenience: allow `python app.py` only for development.
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))

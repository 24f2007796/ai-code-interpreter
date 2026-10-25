import json
import os
import tempfile
from pathlib import Path

# Isolate the SQLite database before importing app.
tmp = tempfile.NamedTemporaryFile(delete=False)
tmp.close()
os.environ["DATABASE_PATH"] = tmp.name

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app as agent
from fastapi.testclient import TestClient

client = TestClient(agent.app)
TOKEN_A = "grader-token-user-a"
TOKEN_B = "grader-token-user-b"


def headers(token=TOKEN_A, version="1.0", content_type=agent.A2A_MEDIA):
    return {"Authorization": f"Bearer {token}", "A2A-Version": version, "Content-Type": content_type}


def fake_decisions(batch_id, policy_revision, packages):
    result = []
    for p in packages:
        pid = p["packageId"]
        refs = ["[CURRENT-1]", "[CURRENT-2]", "[CURRENT-3]"]
        result.append({
            "packageId": pid,
            "action": "hold_invoice",
            "facts": {"vendorName": "Example Vendor", "invoiceNumber": "INV-1", "amountMinor": 12345, "currency": "INR"},
            "evidenceRefs": refs,
            "rationale": f"hold_invoice because verification remains outstanding; evidence {refs[0]} and {refs[1]} support this action.",
        })
    return result


def sample_message(message_id="m-1", batch_id="b-1"):
    return {
        "message": {
            "messageId": message_id,
            "role": "ROLE_USER",
            "parts": [{
                "mediaType": agent.INPUT_MEDIA,
                "data": {"batchId": batch_id, "policyRevision": "policy-1", "packages": [
                    {"packageId": "p-1", "documents": [{"text": "synthetic test package"}]}
                ]},
            }],
        },
        "configuration": {"returnImmediately": False, "historyLength": 20,
                          "acceptedOutputModes": [agent.PROPOSALS_MEDIA, agent.RECEIPTS_MEDIA]},
    }


def setup_function():
    agent.init_db()
    with agent.immediate_transaction() as conn:
        conn.execute("DELETE FROM message_dedup")
        conn.execute("DELETE FROM tasks")
        conn.execute("DELETE FROM decision_cache")


def test_agent_card_is_public_and_contract_fields_exist():
    response = client.get("/.well-known/agent-card.json")
    assert response.status_code == 200
    card = response.json()
    assert card["name"] and card["description"] and card["version"]
    assert isinstance(card["capabilities"], dict)
    assert card["skills"][0]["id"] == "invoice_action_agent"
    assert agent.INPUT_MEDIA in card["defaultInputModes"]
    assert agent.PROPOSALS_MEDIA in card["defaultOutputModes"]
    assert agent.RECEIPTS_MEDIA in card["defaultOutputModes"]


def test_headers_and_authentication():
    bad_version = client.post("/a2a/message:send", headers=headers(version="2.0"), json=sample_message())
    assert bad_version.status_code == 400
    no_auth = client.get("/a2a/tasks", headers={"A2A-Version": "1.0"})
    assert no_auth.status_code == 401
    bad_media = client.post("/a2a/message:send", headers=headers(content_type="application/json"), json=sample_message())
    assert bad_media.status_code == 415


def test_initial_proposal_and_message_replay(monkeypatch):
    monkeypatch.setattr(agent, "call_ai_for_decisions", fake_decisions)
    response = client.post("/a2a/message:send", headers=headers(), json=sample_message())
    assert response.status_code == 200
    task = response.json()["task"]
    assert task["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert len(task["history"]) == 1
    proposal_part = task["artifacts"][0]["parts"][0]
    assert proposal_part["mediaType"] == agent.PROPOSALS_MEDIA
    assert len(proposal_part["data"]["proposals"]) == 1
    replay = client.post("/a2a/message:send", headers=headers(), json=sample_message())
    assert replay.status_code == 200
    assert replay.json()["task"] == task


def test_message_id_conflict(monkeypatch):
    monkeypatch.setattr(agent, "call_ai_for_decisions", fake_decisions)
    assert client.post("/a2a/message:send", headers=headers(), json=sample_message()).status_code == 200
    changed = sample_message()
    changed["message"]["parts"][0]["data"]["packages"][0]["documents"][0]["text"] = "changed"
    response = client.post("/a2a/message:send", headers=headers(), json=changed)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_accepted_results_complete_task_and_rejected_are_not_executed(monkeypatch):
    monkeypatch.setattr(agent, "call_ai_for_decisions", fake_decisions)
    task = client.post("/a2a/message:send", headers=headers(), json=sample_message()).json()["task"]
    proposal = task["artifacts"][0]["parts"][0]["data"]["proposals"][0]
    continuation = {
        "message": {
            "messageId": "m-results",
            "taskId": task["id"],
            "contextId": task["contextId"],
            "role": "ROLE_USER",
            "parts": [{
                "mediaType": agent.RESULTS_MEDIA,
                "data": {"batchId": "b-1", "results": [{
                    "packageId": proposal["packageId"], "actionId": proposal["actionId"],
                    "action": proposal["action"], "outcome": "ACCEPTED", "receiptNonce": "nonce-from-grader"
                }]},
            }],
        }
    }
    response = client.post("/a2a/message:send", headers=headers(), json=continuation)
    assert response.status_code == 200
    completed = response.json()["task"]
    assert completed["status"]["state"] == "TASK_STATE_COMPLETED"
    assert len(completed["artifacts"]) == 2
    receipt = completed["artifacts"][1]["parts"][0]
    assert receipt["mediaType"] == agent.RECEIPTS_MEDIA
    execution = receipt["data"]["executions"][0]
    assert execution["receiptNonce"] == "nonce-from-grader"
    assert execution["actionId"] == proposal["actionId"]


def test_principal_isolation(monkeypatch):
    monkeypatch.setattr(agent, "call_ai_for_decisions", fake_decisions)
    task = client.post("/a2a/message:send", headers=headers(TOKEN_A), json=sample_message()).json()["task"]
    assert client.get("/a2a/tasks", headers=headers(TOKEN_B)).json() == {"tasks": []}
    assert client.get(f"/a2a/tasks/{task['id']}", headers=headers(TOKEN_B)).status_code == 404
    assert client.post(f"/a2a/tasks/{task['id']}:cancel", headers=headers(TOKEN_B)).status_code == 404


def test_cancel_terminal_task_conflict(monkeypatch):
    monkeypatch.setattr(agent, "call_ai_for_decisions", fake_decisions)
    task = client.post("/a2a/message:send", headers=headers(), json=sample_message()).json()["task"]
    cancelled = client.post(f"/a2a/tasks/{task['id']}:cancel", headers=headers())
    assert cancelled.status_code == 200
    assert cancelled.json()["status"]["state"] == "TASK_STATE_CANCELED"
    assert all(a["parts"][0]["mediaType"] != agent.RECEIPTS_MEDIA for a in cancelled.json()["artifacts"])
    second = client.post(f"/a2a/tasks/{task['id']}:cancel", headers=headers())
    assert second.status_code == 409

# A2A Invoice Action Agent

FastAPI implementation of the assignment's A2A 1.0 HTTP+JSON contract. It proposes exactly one typed action per invoice package, cites three bracketed evidence references, waits for the grader's continuation, and records receipts only for `ACCEPTED` results.

## Important safety behavior

- A proposal is not execution permission. The only receipt artifact is created after a valid results continuation.
- Only accepted results become `executions`; rejected results are retained in continuation history only.
- Task, context, principal, batch, package, action ID, action, and nonce are checked against the stored proposal.
- Every Bearer token maps to a separate SHA-256 principal. Raw tokens are not stored.
- Message IDs are deduplicated per principal. The hash covers recursively key-sorted compact JSON of `message` only, not `configuration`.
- A repeated message ID with changed message content returns `409 IDEMPOTENCY_CONFLICT`.
- SQLite transactions use `BEGIN IMMEDIATE` for state transitions. Deploy a single worker/instance with a persistent disk for this SQLite build.
- No bank/payment provider is integrated. "Execution" means recording the accepted action and grader receipt in the A2A task.

## Files

- `app.py` — API, protocol validation, SQLite state, AI decision batching/cache, proposal and receipt lifecycle.
- `tests/test_app.py` — contract, replay, conflict, receipt, and isolation tests.
- `render.yaml` — Render service template with persistent disk.
- `requirements.txt` — dependencies.

## Local setup

Python 3.11+ recommended.

```bash
python -m venv .venv
# Windows PowerShell:
.venv\Scripts\Activate.ps1
# macOS/Linux:
source .venv/bin/activate
pip install -r requirements.txt
```

Set an AI provider key in your terminal (do not commit it):

```powershell
# Windows PowerShell
$env:AI_API_KEY="YOUR_PROVIDER_KEY"
$env:AI_MODEL="gpt-4o-mini"
$env:DATABASE_PATH="invoice_agent.db"
uvicorn app:app --reload --port 8000
```

```bash
# macOS/Linux
export AI_API_KEY="YOUR_PROVIDER_KEY"
export AI_MODEL="gpt-4o-mini"
export DATABASE_PATH="invoice_agent.db"
uvicorn app:app --reload --port 8000
```

`AI_BASE_URL` defaults to `https://api.openai.com/v1`; set it to another OpenAI-compatible endpoint if needed. Do not use real finance records or put secrets in prompts. The model is called once for all uncached packages in a batch; decisions are cached by canonical package content and policy revision. Polls, list, reads, cancellation, conflicts, and result continuations do not call the model.

## Tests

```bash
pytest -q
```

The tests replace the model call with a deterministic test double, so tests do not require a paid model API key.

## Render deployment

1. Commit these files into your existing assignment repository without overwriting unrelated assignment work.
2. Push your feature branch and open a pull request or merge the new files as your course rules allow. Do not force-push or overwrite existing work.
3. In Render, create a Web Service connected to that repository and use:
   - Build command: `pip install -r requirements.txt`
   - Start command: `uvicorn app:app --host 0.0.0.0 --port $PORT --workers 1`
   - Health check path: `/health`
4. Attach a persistent disk mounted at `/var/data`; set `DATABASE_PATH=/var/data/invoice_agent.db`. SQLite on an ephemeral filesystem loses task state after redeploy.
5. Set `AI_API_KEY` (secret), `AI_MODEL=gpt-4o-mini`, and `AI_BASE_URL=https://api.openai.com/v1`.
6. Set `A2A_BASE_URL` to the exact base URL you will submit, e.g. `https://YOUR-SERVICE.onrender.com/a2a` (no query, fragment, or credentials).
7. Verify `https://YOUR-SERVICE.onrender.com/.well-known/agent-card.json` and the health route over HTTPS. The Agent Card's `supportedInterfaces[0].url` must exactly match the submitted base URL.
8. Use a single Render instance and one Uvicorn worker for this SQLite version; multi-instance deployments require a shared transactional database implementation.

### Health and API routes

- `GET /health`
- `GET /.well-known/agent-card.json` (public)
- `POST /a2a/message:send`
- `GET /a2a/tasks`
- `GET /a2a/tasks/{id}`
- `POST /a2a/tasks/{id}:cancel`

All routes except discovery and health require `A2A-Version: 1.0` and a Bearer token. POST routes also require `Content-Type: application/a2a+json`.

## Check/Save plan (10 checks maximum)

- Checks 1–2: card, auth, version/media types, initial proposal envelope.
- Checks 3–4: actions, facts, exactly three decisive references, rationale.
- Checks 5–6: accepted/rejected receipts, task history and terminal replay.
- Checks 7–8: idempotency conflicts, cancellation race and cross-user isolation.
- Check 9: final rehearsal after the last code/deploy fix.
- Save: use the final check only after all known issues are fixed. The grader's feedback should drive the next code change; don't spend checks on speculative changes.

This is a starter implementation; treat the grader's feedback as authoritative and fix contract mismatches before Save.

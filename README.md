# Bank Mortgage Cycle — AI-Native Mortgage

An agentic solution design for AI-native mortgage origination: exception handling,
human-in-the-loop supervisor approvals, and a tamper-evident audit trail, built
against Claude on Microsoft Foundry.

The repo has three layers, in the order they were built:

| Path | What it is | Contains AI? |
|---|---|---|
| `Coforge AI-Native Mortgage Demo.html` | Reference design — the target solution's UI/UX | No |
| `mvp/index.html` | Zero-dependency clickable MVP of the design | No |
| `poc/` | Working slice: real agents against Claude Opus 4.7 on Microsoft Foundry, gated tools, enforced constraints, audit trail | Yes |
| `azure-web-app/` | Deployment reference notes for hosting the POC on Azure | — |
| `.claude/skills/ai-native-mortgage/` | Domain model, agent roster, seed data used to author the solution | — |

---

## 1. Reference design & MVP (no setup required)

Both are static, self-contained HTML files — no build step, no server, no
dependencies. Open directly in a browser:

```bash
# Reference design (target UI/UX, no AI)
open "Coforge AI-Native Mortgage Demo.html"    # macOS
start "Coforge AI-Native Mortgage Demo.html"   # Windows

# Clickable MVP
open mvp/index.html
start mvp/index.html
```

---

## 2. POC — agentic backend + frontend

Full setup, architecture, and design rationale live in
[`poc/README.md`](poc/README.md). Quick start:

### Prerequisites
- Python 3.14 (the backend lock file and Docker image are verified against it)
- Node.js 18+
- A Claude deployment on Microsoft Foundry (API key + endpoint)

### Backend

```bash
cd poc
python -m venv .venv
.venv/Scripts/activate            # Windows; source .venv/bin/activate on macOS/Linux

pip install --no-deps -r backend/requirements-dev.lock.txt

cp .env.example .env              # fill in FOUNDRY_API_KEY and FOUNDRY_API_ENDPOINT
```

Run the day-one Foundry connectivity spike, then generate the synthetic loan book:

```bash
python scripts/spike_foundry.py
python scripts/generate_synthetic_data.py     # writes backend/data/
python scripts/verify_synthetic_data.py       # 147 checks
```

Start the API:

```bash
cd backend && python -m uvicorn app.api:app --reload   # http://127.0.0.1:8000
```

### Frontend

```bash
cd poc/frontend
npm install
npm run dev                                    # http://localhost:5173 (proxies /api to :8000)
```

### Tests

```bash
cd poc/backend && pytest
cd poc/frontend && npm test
```

See [`poc/TESTING.md`](poc/TESTING.md) for the full test plan and
[`poc/README.md`](poc/README.md) for how secrets are handled per environment
(local `.env`, Docker `--env-file`, Azure Key Vault), the agent/tool/policy
design, and the prompt-injection containment walkthrough.

---

## 3. Azure deployment

Reusable Azure hosting notes (resource group, App Service plan, container
registry, deployment steps) are in
[`azure-web-app/AZURE_DEPLOYMENT_NOTES.md`](azure-web-app/AZURE_DEPLOYMENT_NOTES.md).

---

## Secrets

Never commit `poc/.env` or any API key. `poc/.gitignore` excludes `.env`,
`*.key`, `*.pem`, and database files. Copy `poc/.env.example` and fill in your
own credentials locally.

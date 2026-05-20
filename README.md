# Agentic Test Suite Maintenance System

A FastAPI application that automates the maintenance of API test suites by:
1. Parsing ExtentReport HTML test results
2. Classifying failures with LLM guardrails (Gemini) — separating safe assertion updates from genuine backend bugs
3. Presenting a human-in-the-loop review UI for approval
4. Automatically creating a GitHub Pull Request with the approved fixes

## Architecture

```
Browser → FastAPI → MongoDB (Motor)
                 ↓
              Huey (SQLite broker)
                 ↓
    ┌────────────┬─────────────────┐
    │            │                 │
 Parse HTML  Classify (Gemini)  Fix + PR (Gemini + GitPython)
```

## Prerequisites

- Python 3.11+
- MongoDB 6+ (local via Docker Compose, or Atlas)
- A GitHub Personal Access Token with scopes: `repo`, `pull_requests`
- A Google Gemini API key

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Start MongoDB

```bash
docker-compose up -d
```

### 3. Configure environment

Edit `.env` with your values:

```env
MONGODB_URL=mongodb://localhost:27017
DB_NAME=test_suite_manager
SECRET_KEY=<generate a long random string>
GOOGLE_API_KEY=<your Gemini API key>
LLM_MODEL_NAME=gemini-1.5-pro
GITHUB_PAT=<your GitHub PAT>
GITHUB_REPO_OWNER=<org or username>
GITHUB_REPO_NAME=<target repository name>
GITHUB_DEFAULT_BRANCH=main
```

Generate a secure `SECRET_KEY`:
```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

### 4. Start the application (two terminals required)

**Terminal 1 — FastAPI web server:**
```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

**Terminal 2 — Huey background worker:**
```bash
huey_consumer app.tasks.huey_tasks.huey
```

Open [http://localhost:8000](http://localhost:8000) in your browser.

## Workflow

1. **Register / Login** at `/register`
2. **Upload** an ExtentReport `.html` file from the Dashboard
3. The system automatically:
   - Parses failed tests from the HTML
   - Fetches each test's Java source from GitHub
   - Classifies each failure using Gemini with 3-point guardrails
4. **Review** classifications in the Job Detail page:
   - `SAFE_TO_FIX` — negative/error scenario where only the expected value changed
   - `BACKEND_BUG` — happy-path regression or security test failure (excluded from fixes)
   - `UNCLASSIFIED` — classification failed (file not found, LLM error, etc.)
5. **Approve** the `SAFE_TO_FIX` items and click **Execute Fixes & Create PR**
6. The system clones the repo, applies LLM-generated assertion fixes, pushes a branch, and creates a PR

## Validation Scripts

Before running the full pipeline, validate each layer independently:

```bash
# Validate parser selectors against your actual report
python scripts/test_parser.py path/to/your/report.html

# Validate LLM classification with known test cases
python scripts/test_classifier.py

# Validate GitHub API connectivity
python scripts/test_github.py OrderApiTest

# Full GitHub round-trip (creates and deletes a real branch + draft PR)
python scripts/test_github.py OrderApiTest --full-roundtrip
```

## Parser Calibration

The HTML parser (`app/services/parser_service.py`) is built against the standard
ExtentReport v5 (Spark reporter) DOM structure. If your report uses a different
structure, run `scripts/test_parser.py` against a real report and adjust the
selectors in `parser_service.py`.

Key selectors to check:
- `attrs={"status": "fail"}` — failed test nodes
- `class_="test-name"` — test name element
- `class_="log-details"` or `class_="log-message"` — log entries
- `data-classname` attribute — Java class name

## Project Structure

```
app/
├── main.py                 FastAPI entry point + lifespan (MongoDB indexes)
├── config.py               Pydantic settings (reads .env)
├── database.py             Motor async MongoDB client
├── dependencies.py         get_current_user FastAPI dependency
├── models/
│   ├── user.py             UserInDB model
│   ├── job.py              Job + JobStatus enum
│   └── test_failure.py     TestFailure + Classification enum
├── routers/
│   ├── auth.py             /login /logout /register
│   ├── dashboard.py        /dashboard
│   └── jobs.py             /jobs/* endpoints
├── services/
│   ├── auth_service.py     JWT + bcrypt utilities
│   ├── parser_service.py   BeautifulSoup ExtentReport parser
│   ├── classifier_service.py  LangChain Gemini classification chain
│   ├── fixer_service.py    LangChain Gemini fixer chain
│   └── github_service.py   PyGithub + GitPython operations
├── tasks/
│   └── huey_tasks.py       Huey task definitions
└── templates/
    ├── base.html
    ├── login.html
    ├── register.html
    ├── dashboard.html
    └── job_detail.html
scripts/
├── test_parser.py          Offline parser validation
├── test_classifier.py      Offline classifier validation
└── test_github.py          Offline GitHub API validation
reports/                    Uploaded HTML report storage
repos/                      Temporary local git clones (auto-cleaned)
```

## Known Limitations (POC)

- **No WebSockets** — dashboard and job detail pages auto-refresh via `<meta http-equiv="refresh">` (every 10s / 8s respectively)
- **Single-repo PAT** — all operations use a single GitHub PAT; no per-user GitHub auth
- **SQLite broker** — Huey uses SQLite which is not suitable for multi-process/multi-host deployments; upgrade to Redis for production
- **Synchronous Huey tasks** — Motor (async) calls inside tasks use `asyncio.run()` which opens a new event loop per task; acceptable for POC throughput
- **No pagination** — dashboard shows the 50 most recent jobs
- **Parser calibration required** — HTML selectors must be validated against your specific ExtentReport version before use

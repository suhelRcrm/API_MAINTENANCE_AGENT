# Agentic Test Suite Maintenance System

A FastAPI application that automates the maintenance of API test suites. It ingests failed API test reports, uses Gemini to decide what can be auto-fixed, and opens a GitHub PR with targeted test updates.

**Core mission:** sync tests to the API — not change production code.

---

## What It Does

1. **Upload** an ExtentReport `.html` from the dashboard.
2. **Parse** failed tests (name, assertion error, curl, response, class path).
3. **Classify** each failure with Gemini: `SAFE_TO_FIX`, `BACKEND_BUG`, or `UNCLASSIFIED`.
4. **Review** results on the job detail page (human-in-the-loop UI).
5. **Execute fixes** — clone repo, apply LLM-generated patches, push branch, create PR.
6. **Track status** per failure (`fix_status`: FIXED / SKIPPED / FAILED).

### End-to-End Pipeline

```
User uploads ExtentReport HTML
        ↓
task_parse_report → Extract failed tests + metadata
        ↓
task_classify_failures → Gemini classifies each failure
        ↓
    ┌───────────────┬────────────────┬─────────────────┐
    │ SAFE_TO_FIX   │ BACKEND_BUG    │ UNCLASSIFIED    │
    │ (auto-fix)    │ (excluded)     │ (needs review)  │
    └───────────────┴────────────────┴─────────────────┘
        ↓
task_execute_fixes → Clone GitHub repo
        ↓
Gemini generates surgical fixes → Apply patches to Java + JSON schema files
        ↓
Commit, push branch, create PR → Job COMPLETED with PR URL
```

### Architecture

```
Browser → FastAPI → MongoDB (Motor)
                 ↓
              Huey (SQLite broker)
                 ↓
    ┌────────────┬─────────────────┐
    │            │                 │
 Parse HTML  Classify (Gemini)  Fix + PR (Gemini + GitPython)
```

---

## Prerequisites

- Python 3.11+
- MongoDB 6+ (local via Docker Compose, or Atlas)
- A GitHub Personal Access Token with scopes: `repo`, `pull_requests`
- A Google Gemini API key

---

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

---

## Workflow

1. **Register / Login** at `/register`
2. **Upload** an ExtentReport `.html` file from the Dashboard
3. The system automatically:
   - Parses failed tests from the HTML
   - Classifies each failure using Gemini with guardrails
4. **Review** classifications in the Job Detail page:
   - `SAFE_TO_FIX` — test is stale; update assertions to match current API
   - `BACKEND_BUG` — genuine regression or security failure (excluded from fixes)
   - `UNCLASSIFIED` — classification failed (file not found, LLM error, etc.)
5. **SAFE_TO_FIX** items are auto-approved and fixes run automatically
6. The system clones the repo, applies LLM-generated assertion fixes, pushes a branch, and creates a PR

### Smart Retry

Failed jobs can be retried from the stage where they failed (`last_failed_stage`):

| Failed stage | Retry behavior |
|--------------|----------------|
| `parse` | Full restart — re-parse report |
| `classify` | Resume classification — skip already-labeled rows |
| `execute` | Re-run fixes only — skip parse & classify |

---

## Project Directory Structure

```
API_MAINTENANCE_AGENT/
│
├── app/                          # Main Python application package
│   ├── main.py                   # FastAPI entry point, lifespan, router wiring
│   ├── config.py                 # Pydantic settings from .env
│   ├── database.py               # Motor (async MongoDB) client + collections
│   ├── dependencies.py           # Auth dependency (JWT cookie → user)
│   ├── logger.py                 # Centralized colored logging
│   │
│   ├── models/                   # Pydantic data models (MongoDB documents)
│   │   ├── user.py               # UserInDB
│   │   ├── job.py                # Job, JobStatus, JobFailedStage
│   │   └── test_failure.py       # TestFailure, Classification, FixStatus
│   │
│   ├── routers/                  # FastAPI HTTP routes (server-rendered HTML)
│   │   ├── auth.py               # /login, /logout, /register
│   │   ├── dashboard.py          # /, /dashboard
│   │   └── jobs.py               # /jobs/upload, /jobs/{id}, retry, delete
│   │
│   ├── services/                 # Business logic layer
│   │   ├── auth_service.py       # bcrypt + JWT helpers
│   │   ├── parser_service.py     # ExtentReport HTML parser (BeautifulSoup)
│   │   ├── classifier_service.py # Gemini classification (LangChain)
│   │   ├── fixer_service.py      # Gemini fixer + patch application
│   │   ├── github_service.py     # PyGithub + GitPython (clone, commit, PR)
│   │   └── job_pipeline.py       # Shared pipeline helpers (classified checks)
│   │
│   ├── tasks/
│   │   └── huey_tasks.py         # Background job queue (parse → classify → fix)
│   │
│   ├── templates/                # Jinja2 HTML templates
│   │   ├── base.html
│   │   ├── login.html
│   │   ├── register.html
│   │   ├── dashboard.html
│   │   └── job_detail.html
│   │
│   └── static/
│       └── css/main.css
│
├── scripts/                      # Offline validation scripts
│   ├── test_parser.py            # Test parser against a real HTML report
│   ├── test_classifier.py        # Test Gemini classification
│   └── test_github.py            # Test GitHub API + optional full round-trip
│
├── reports/                      # Uploaded ExtentReport HTML files (runtime)
├── repos/                        # Temporary local git clones per job (runtime)
├── huey_storage.db               # Huey SQLite task queue (created at runtime)
│
├── docker-compose.yml            # MongoDB container
├── requirements.txt              # Python dependencies
├── .env                          # Secrets + config (not committed)
└── README.md
```

Runtime folders (`reports/`, `repos/`, `huey_storage.db`) are created during use, not part of the source package.

---

## Tech Stack

| Layer | Technology | Package |
|-------|------------|---------|
| Web framework | FastAPI + Uvicorn | `fastapi`, `uvicorn` |
| Templates | Jinja2 | `jinja2` |
| Database | MongoDB via Motor | `motor`, `pymongo` |
| Background jobs | Huey (SQLite broker) | `huey[sqlite]` |
| LLM | Google Gemini via LangChain | `langchain`, `langchain-google-genai` |
| HTML parsing | BeautifulSoup + lxml | `beautifulsoup4`, `lxml` |
| GitHub API | PyGithub | `PyGithub` |
| Local git ops | GitPython | `GitPython` |
| Auth | JWT + bcrypt | `python-jose`, `bcrypt` |
| Config | Pydantic Settings | `pydantic-settings` |

---

## Package-by-Package Breakdown

### `app/main.py` — Application Entry

- Creates FastAPI app with lifespan hooks.
- On startup: connects MongoDB, creates indexes (`username`, `job_id`, etc.).
- Mounts static files at `/static`.
- Registers routers: `auth`, `jobs`, `dashboard`.

### `app/config.py` — Settings

Reads from `.env`:

| Setting | Purpose |
|---------|---------|
| `MONGODB_URL`, `DB_NAME` | MongoDB connection |
| `SECRET_KEY`, `ALGORITHM` | JWT auth |
| `GOOGLE_API_KEY`, `LLM_MODEL_NAME` | Gemini |
| `classify_batch_size`, `classify_batch_delay_seconds` | Rate-limit protection |
| `fixer_max_workers`, `fixer_llm_parallel_per_file` | Parallel fix execution |
| `GITHUB_PAT`, `GITHUB_REPO_OWNER`, `GITHUB_REPO_NAME` | Target test repo |
| `git_clone_max_retries`, `github_clone_depth` | Reliable cloning |
| `reports_dir`, `repos_dir` | Local storage paths |

### `app/database.py` — MongoDB

Three collections:

- `users` — registered users
- `jobs` — one per uploaded report
- `test_failures` — one row per failed test in a job

### `app/models/` — Data Models

**`Job`**

```
JobStatus: PARSING → PENDING_CLASSIFICATION → EXECUTING_FIXES → COMPLETED | FAILED
JobFailedStage: parse | classify | execute  (for smart retry)
```

**`TestFailure`**

```
Classification: SAFE_TO_FIX | BACKEND_BUG | UNCLASSIFIED
FixStatus: NOT_APPLICABLE | PENDING | FIXED | SKIPPED | FAILED
```

Fields include: `test_name`, `test_class_path`, `curl_command`, `actual_response`, `assertion_error`, `classification`, `llm_reasoning`, `user_approved`, `fix_status`, `fix_reason`.

### `app/routers/` — HTTP API + UI

| Route | File | What it does |
|-------|------|--------------|
| `GET /login`, `POST /login` | `auth.py` | Login, sets JWT cookie |
| `GET /register`, `POST /register` | `auth.py` | User registration |
| `POST /logout` | `auth.py` | Clears cookie |
| `GET /dashboard` | `dashboard.py` | Lists recent jobs |
| `POST /jobs/upload` | `jobs.py` | Upload HTML, create job, enqueue parse |
| `GET /jobs/{job_id}` | `jobs.py` | Job detail + all failures |
| `POST /jobs/{job_id}/retry` | `jobs.py` | Smart retry from failed stage |
| `POST /jobs/{job_id}/delete` | `jobs.py` | Delete job, report, clone |

### `app/services/` — Core Business Logic

#### `parser_service.py`

Parses **ExtentReport v5 (Spark)** HTML:

- Finds failed test nodes (`status="fail"`).
- Extracts: test name, curl command, HTTP response, assertion error.
- Resolves `test_class_path` via fallbacks:
  1. `Script failed at method: ClassName.methodName`
  2. `testClass` in curl JSON
  3. Stack trace (filtering JDK/library frames)
- Strips data-provider suffixes from test names.

#### `classifier_service.py`

Gemini LLM classification with mission: **sync tests to API**.

**SAFE_TO_FIX patterns** (examples):

- Error message / status code text changed (negative tests)
- JSON schema nullability drift
- Enum/list size grew
- JSON key renamed (`message` → `messages`)
- NPE/CCE/IOOBE from stale JSON paths

**BACKEND_BUG:** real regressions, security failures, wrong business outcomes.

Runs in **chunks** (default 75) with delays and 429 retry to avoid quota exhaustion.

#### `fixer_service.py`

Gemini generates **surgical replacements**, not full file rewrites:

```json
{
  "replacements": [
    {"old_snippet": "...", "new_snippet": "...", "description": "..."}
  ],
  "schema_updates": [...]
}
```

**Fix strategies:**

- **A** — Update error message / status code
- **B** — Update JSON schema file (not Java)
- **C** — Enum/list size assertions
- **D** — JSON key rename / path change
- **E** — Semantically equivalent message rewording
- **G** — NPE/CCE from stale paths

`apply_replacements()` does literal `.replace(old, new, 1)` so PR diffs stay minimal.

Parallel execution:

- `fix_failures_for_java_file()` — parallel LLM calls per file
- `process_java_file_patches()` — parallel across files

#### `github_service.py`

| Function | Purpose |
|----------|---------|
| `fetch_java_file()` | Read source from GitHub API |
| `resolve_file_path()` | Map class path → repo-relative path |
| `clone_repository()` | Clone with retry + shallow depth |
| `write_fixed_file()` | Write patched content to working tree |
| `commit_and_push_fixes()` | Branch, commit, push |
| `create_pull_request()` | Open PR via PyGithub |
| `remove_local_repo()` | Windows-safe cleanup |

#### `job_pipeline.py`

Shared helpers:

- `is_failure_classified()` — skip already-classified rows on retry
- `all_failures_classified()`, `count_unclassified()`

#### `auth_service.py`

- `hash_password()` / `verify_password()` — bcrypt
- `create_access_token()` / `decode_access_token()` — JWT

### `app/tasks/huey_tasks.py` — Background Pipeline

Three Huey tasks chained together:

| Task | Trigger | Action |
|------|---------|--------|
| `task_parse_report` | Upload | Parse HTML → insert failures → chain classify |
| `task_classify_failures` | After parse | Gemini classify in chunks → chain execute |
| `task_execute_fixes` | After classify | Clone, patch, commit, push, create PR |

Each task uses `asyncio.run()` with its own Motor client (Huey is sync; Motor is async).

---

## Data Flow (Detailed)

```
1. UPLOAD
   User → POST /jobs/upload
   → Save HTML to reports/{job_id}_report.html
   → Insert Job (status=PARSING)
   → Enqueue task_parse_report

2. PARSE
   BeautifulSoup reads HTML
   → For each failed test: TestFailure document
   → Insert into test_failures collection
   → Job status = PENDING_CLASSIFICATION
   → Enqueue task_classify_failures

3. CLASSIFY
   For each unclassified failure:
   → Send batch to Gemini (test_name, assertion_error, truncated response)
   → Persist classification + llm_reasoning
   → If SAFE_TO_FIX exists: auto-approve, status=EXECUTING_FIXES
   → Enqueue task_execute_fixes

4. EXECUTE FIXES
   → Clone target repo to repos/job_{job_id}/
   → Group failures by Java file
   → For each file: fetch source, run fixer LLM, apply replacements
   → For schema fixes: update JSON under src/test/resources/
   → write_fixed_file() for each changed file
   → commit_and_push_fixes() on branch fix/job-{job_id}-{timestamp}
   → create_pull_request()
   → Job status = COMPLETED, github_pr_url set
   → Per-failure fix_status: FIXED / SKIPPED / FAILED
```

---

## MongoDB Schema

**`users`**

```json
{ "id", "username", "password_hash", "created_at" }
```

**`jobs`**

```json
{
  "id", "user_id", "report_file_path",
  "status",
  "github_pr_url",
  "error_message",
  "last_failed_stage",
  "created_at", "updated_at"
}
```

**`test_failures`**

```json
{
  "id", "job_id",
  "test_name", "test_class_path",
  "curl_command", "actual_response", "assertion_error",
  "classification",
  "llm_reasoning",
  "user_approved",
  "fix_status", "fix_reason"
}
```

---

## External Integrations

| Service | Used For | Where |
|---------|----------|-------|
| **MongoDB** | Job + failure persistence | `database.py`, all tasks |
| **Google Gemini** | Classify + fix generation | `classifier_service.py`, `fixer_service.py` |
| **GitHub API (PyGithub)** | Fetch files, create branch/PR | `github_service.py` |
| **Git (GitPython)** | Clone, commit, push | `github_service.py`, `huey_tasks.py` |
| **Target test repo** | Java tests + JSON schemas | Configured via `GITHUB_REPO_*` |

The app does **not** contain your test code — it points at an external GitHub repo (e.g. your API test suite) and patches files there.

---

## UI Pages

| Page | Template | Purpose |
|------|----------|---------|
| Login | `login.html` | Authenticate |
| Register | `register.html` | Create account |
| Dashboard | `dashboard.html` | Upload report, list jobs |
| Job Detail | `job_detail.html` | View classifications, fix status, PR link, retry |

Pages auto-refresh via `<meta http-equiv="refresh">` (no WebSockets in this POC).

---

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

---

## Parser Calibration

The HTML parser (`app/services/parser_service.py`) is built against the standard ExtentReport v5 (Spark reporter) DOM structure. If your report uses a different structure, run `scripts/test_parser.py` against a real report and adjust the selectors in `parser_service.py`.

Key selectors to check:

- `attrs={"status": "fail"}` — failed test nodes
- `class_="test-name"` — test name element
- `class_="log-details"` or `class_="log-message"` — log entries
- `data-classname` attribute — Java class name

---

## Design Principles

1. **Sync tests to API** — default assumption is stale tests, not broken APIs.
2. **Surgical diffs** — LLM returns `old_snippet`/`new_snippet` pairs, not full files.
3. **Human review** — UI shows classifications; `BACKEND_BUG` never auto-fixed.
4. **Resumable jobs** — `last_failed_stage` + skip already-classified on retry.
5. **Rate-limit aware** — chunked LLM calls, backoff on 429.
6. **Parallel fixing** — ThreadPoolExecutor across files and per-file LLM calls.

---

## Known Limitations (POC)

- **No WebSockets** — dashboard and job detail pages auto-refresh via `<meta http-equiv="refresh">` (every 10s / 8s respectively)
- **Single-repo PAT** — all operations use a single GitHub PAT; no per-user GitHub auth
- **SQLite broker** — Huey uses SQLite which is not suitable for multi-process/multi-host deployments; upgrade to Redis for production
- **Synchronous Huey tasks** — Motor (async) calls inside tasks use `asyncio.run()` which opens a new event loop per task; acceptable for POC throughput
- **No pagination** — dashboard shows the 100 most recent jobs
- **Parser calibration required** — HTML selectors must be validated against your specific ExtentReport version before use
- **Schema fixes** — JSON schema updates may reformat via `json.dumps(indent=2)` if the file was not already 2-space indented

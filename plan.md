# Execution Plan: Agentic Test Suite Maintenance System

## Senior Staff Engineer Review & Notes

Before diving in, a few architectural observations that will shape the plan:

- **Huey + SQLite** is the right call for a POC — avoid over-engineering with Redis/Celery.
- **PyGithub's file-fetch pattern** (via `repo.get_contents()`) will be used for classification *before* we clone — this avoids unnecessary full clones during Phase 2.
- **Phase 4 git operations** will use a hybrid: PyGithub API for branch creation/PR, but `GitPython` locally for actual file mutation + commit + push (the GitHub API is painful for multi-file commits).
- The guardrail logic (Phase 2) is the most critical piece — it will be implemented as a **structured LLM chain with a Pydantic output parser**, not freeform text, to prevent hallucinated classifications.

---

## Phase 0: Project Scaffolding & Environment Setup

### Task 0.1 — Initialize Repository & Project Structure
Create the following directory layout:

```
project-root/
├── app/
│   ├── main.py                  # FastAPI entry point
│   ├── config.py                # Settings via pydantic-settings
│   ├── database.py              # Motor async MongoDB client
│   ├── models/
│   │   ├── user.py
│   │   ├── job.py
│   │   └── test_failure.py
│   ├── routers/
│   │   ├── auth.py
│   │   ├── jobs.py
│   │   └── dashboard.py
│   ├── services/
│   │   ├── parser_service.py    # BeautifulSoup HTML parsing
│   │   ├── classifier_service.py # LangChain classification chain
│   │   ├── github_service.py    # PyGithub + GitPython operations
│   │   └── fixer_service.py     # LangChain code rewrite chain
│   ├── tasks/
│   │   └── huey_tasks.py        # All Huey task definitions
│   ├── templates/               # Jinja2 HTML templates
│   │   ├── base.html
│   │   ├── login.html
│   │   ├── dashboard.html
│   │   └── job_detail.html
│   └── static/                  # CSS/JS assets
├── reports/                     # Uploaded HTML report storage
├── repos/                       # Temporary local git clones (Phase 4)
├── huey_storage.db              # SQLite broker file
├── requirements.txt
├── .env
└── docker-compose.yml           # MongoDB for local dev
```

### Task 0.2 — Install Dependencies & Pin Versions
Populate `requirements.txt`:

```
fastapi
uvicorn[standard]
jinja2
python-multipart          # for file upload forms
motor                     # async MongoDB driver
pymongo
pydantic-settings
python-jose[cryptography] # JWT
passlib[bcrypt]           # password hashing
huey[sqlite]              # task queue with SQLite broker
langchain
langchain-google-genai    # or langchain-openai
beautifulsoup4
lxml                      # faster BS4 parser
PyGithub
GitPython
python-dotenv
```

### Task 0.3 — Configure Environment Variables
Populate `.env`:

```env
MONGODB_URL=mongodb://localhost:27017
DB_NAME=test_suite_manager
SECRET_KEY=<generate-256bit-random>
ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=480
LLM_API_KEY=<gemini-or-openai-key>
LLM_MODEL_NAME=gemini-1.5-pro
GITHUB_PAT=<service-account-pat>
GITHUB_REPO_OWNER=<org-or-user>
GITHUB_REPO_NAME=<target-repo>
GITHUB_DEFAULT_BRANCH=main
REPORTS_DIR=./reports
REPOS_DIR=./repos
```

### Task 0.4 — Bootstrap `config.py` with Pydantic Settings

```python
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    mongodb_url: str
    db_name: str
    secret_key: str
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 480
    llm_api_key: str
    llm_model_name: str
    github_pat: str
    github_repo_owner: str
    github_repo_name: str
    github_default_branch: str = "main"
    reports_dir: str = "./reports"
    repos_dir: str = "./repos"

    class Config:
        env_file = ".env"

settings = Settings()
```

### Task 0.5 — Bootstrap MongoDB with Motor in `database.py`

```python
from motor.motor_asyncio import AsyncIOMotorClient
from app.config import settings

client = AsyncIOMotorClient(settings.mongodb_url)
db = client[settings.db_name]

# Collection handles
users_collection = db["users"]
jobs_collection = db["jobs"]
failures_collection = db["test_failures"]
```

Create MongoDB indexes on startup (via FastAPI `lifespan`):
- `users`: unique index on `username`
- `jobs`: index on `user_id`, `status`
- `test_failures`: index on `job_id`, `classification`

---

## Phase 1: Data Models

### Task 1.1 — Define Pydantic/Motor Models for `User`

```python
# app/models/user.py
from pydantic import BaseModel, Field
from bson import ObjectId
from datetime import datetime

class UserInDB(BaseModel):
    id: str = Field(default_factory=lambda: str(ObjectId()))
    username: str
    password_hash: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
```

### Task 1.2 — Define `Job` Model with Full Status Enum

```python
# app/models/job.py
from enum import Enum

class JobStatus(str, Enum):
    PARSING = "PARSING"
    PENDING_CLASSIFICATION = "PENDING_CLASSIFICATION"
    AWAITING_USER_APPROVAL = "AWAITING_USER_APPROVAL"
    EXECUTING_FIXES = "EXECUTING_FIXES"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"

class Job(BaseModel):
    id: str = Field(default_factory=lambda: str(ObjectId()))
    user_id: str
    report_file_path: str
    status: JobStatus = JobStatus.PARSING
    github_pr_url: Optional[str] = None
    error_message: Optional[str] = None   # capture failure reason
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
```

### Task 1.3 — Define `TestFailure` Model with Classification Enum

```python
# app/models/test_failure.py
class Classification(str, Enum):
    SAFE_TO_FIX = "SAFE_TO_FIX"
    BACKEND_BUG = "BACKEND_BUG"
    UNCLASSIFIED = "UNCLASSIFIED"

class TestFailure(BaseModel):
    id: str = Field(default_factory=lambda: str(ObjectId()))
    job_id: str
    test_name: str
    test_class_path: str          # e.g. "com/example/tests/OrderApiTest.java"
    curl_command: Optional[str]
    actual_response: Optional[str]
    assertion_error: Optional[str]
    classification: Classification = Classification.UNCLASSIFIED
    llm_reasoning: Optional[str] = None
    user_approved: bool = False
```

---

## Phase 2: Authentication System

### Task 2.1 — Implement Password Hashing Utilities

```python
# app/services/auth_service.py
from passlib.context import CryptContext
from jose import jwt
from datetime import datetime, timedelta
from app.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def hash_password(password: str) -> str:
    return pwd_context.hash(password)

def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)

def create_access_token(data: dict) -> str:
    expire = datetime.utcnow() + timedelta(minutes=settings.access_token_expire_minutes)
    return jwt.encode({**data, "exp": expire}, settings.secret_key, algorithm=settings.algorithm)
```

### Task 2.2 — Build JWT Cookie Dependency

Create a `get_current_user` FastAPI dependency that:
1. Reads the `access_token` cookie from the request
2. Decodes and validates the JWT
3. Looks up the user in MongoDB
4. Raises `HTTPException(303)` redirecting to `/login` if invalid — this is critical for Jinja2 flows (not a JSON 401)

### Task 2.3 — Build Auth Router (`/login`, `/logout`, `/register`)

- `GET /login` → renders `login.html` Jinja2 template
- `POST /login` → validates credentials, sets `access_token` cookie, redirects to `/dashboard`
- `POST /logout` → clears cookie, redirects to `/login`
- `POST /register` → creates user with hashed password (admin-only or open for POC)

### Task 2.4 — Build `login.html` Jinja2 Template
Bootstrap 5 card centered on page. Fields: `username`, `password`. Error flash message display.

---

## Phase 3: Huey Task Queue Setup

### Task 3.1 — Initialize Huey with SQLite Broker

```python
# app/tasks/huey_tasks.py
from huey import SqliteHuey

huey = SqliteHuey(filename="huey_storage.db")
```

### Task 3.2 — Integrate Huey with FastAPI Lifespan

In `main.py`, ensure Huey consumer is startable independently:
```bash
# Run worker in separate terminal
huey_consumer app.tasks.huey_tasks.huey
```

Document this in a `Makefile` or `README` with two commands: `uvicorn` + `huey_consumer`.

### Task 3.3 — Define Task Stubs (to be implemented later)

```python
@huey.task()
def task_parse_report(job_id: str): ...

@huey.task()
def task_classify_failures(job_id: str): ...

@huey.task()
def task_execute_fixes(job_id: str, approved_failure_ids: list[str]): ...
```

Each task wraps its logic in a `try/except` that sets `Job.status = FAILED` and writes `error_message` to MongoDB on any unhandled exception.

---

## Phase 4: File Upload & Job Creation

### Task 4.1 — Build Upload Endpoint

```python
# POST /jobs/upload
@router.post("/jobs/upload")
async def upload_report(
    file: UploadFile = File(...),
    current_user: UserInDB = Depends(get_current_user)
):
    job_id = str(ObjectId())
    file_path = f"{settings.reports_dir}/{job_id}_report.html"

    # Save file to disk
    os.makedirs(settings.reports_dir, exist_ok=True)
    with open(file_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    # Create Job in MongoDB
    job = Job(id=job_id, user_id=current_user.id, report_file_path=file_path)
    await jobs_collection.insert_one(job.dict())

    # Dispatch Huey task
    task_parse_report(job_id)

    return RedirectResponse(f"/jobs/{job_id}", status_code=303)
```

Validate file extension is `.html` before saving. Return a `400` with a Jinja2-rendered error if not.

### Task 4.2 — Build Dashboard Template (`dashboard.html`)

- Navbar with username + logout button
- Upload form (`enctype="multipart/form-data"`)
- Table of recent jobs for `current_user` showing: Job ID, Status (color-coded badge), Created At, PR Link (if available)
- Auto-refresh via `<meta http-equiv="refresh" content="10">` for polling simplicity (no WebSockets needed for POC)

---

## Phase 5: HTML Report Parsing (Phase 1 of Workflow)

### Task 5.1 — Implement `parser_service.py`

This is pure BeautifulSoup logic. Study a sample ExtentReport HTML to identify the DOM structure. Typical ExtentReport patterns:

```python
# app/services/parser_service.py
from bs4 import BeautifulSoup
from app.models.test_failure import TestFailure

def parse_extent_report(file_path: str, job_id: str) -> list[TestFailure]:
    with open(file_path, "r", encoding="utf-8") as f:
        soup = BeautifulSoup(f, "lxml")

    failures = []
    # ExtentReport marks failed tests with class "fail" or attribute status="fail"
    for test_node in soup.find_all(attrs={"status": "fail"}):
        test_name = test_node.find(class_="test-name").get_text(strip=True)
        
        # Extract log details — CURL, actual response, assertion error
        logs = test_node.find_all(class_="log-details")
        curl_command, actual_response, assertion_error = None, None, None
        
        for log in logs:
            text = log.get_text(strip=True)
            if text.startswith("curl"):
                curl_command = text
            elif "AssertionError" in text or "expected" in text.lower():
                assertion_error = text
            elif "{" in text or text.startswith("HTTP"):
                actual_response = text
        
        # test_class_path: extracted from test metadata or a dedicated field
        class_path = test_node.get("data-classname", "").replace(".", "/") + ".java"

        failures.append(TestFailure(
            job_id=job_id,
            test_name=test_name,
            test_class_path=class_path,
            curl_command=curl_command,
            actual_response=actual_response,
            assertion_error=assertion_error
        ))
    return failures
```

**Important:** The exact selectors will need adjustment against a real ExtentReport sample. Build a small offline test script against one real report before wiring into Huey.

### Task 5.2 — Implement `task_parse_report` Huey Task

```python
@huey.task()
def task_parse_report(job_id: str):
    # Note: Huey tasks are sync; use asyncio.run() for Motor calls
    import asyncio
    
    async def _run():
        job = await jobs_collection.find_one({"id": job_id})
        try:
            failures = parse_extent_report(job["report_file_path"], job_id)
            if not failures:
                raise ValueError("No failed tests found in report")
            
            await failures_collection.insert_many([f.dict() for f in failures])
            await jobs_collection.update_one(
                {"id": job_id},
                {"$set": {"status": JobStatus.PENDING_CLASSIFICATION, 
                           "updated_at": datetime.utcnow()}}
            )
            # Chain to next task
            task_classify_failures(job_id)
        except Exception as e:
            await jobs_collection.update_one(
                {"id": job_id},
                {"$set": {"status": JobStatus.FAILED, "error_message": str(e)}}
            )
    
    asyncio.run(_run())
```

---

## Phase 6: LLM Classification with Guardrails (Phase 2 of Workflow)

This is the most architecturally sensitive phase. The guardrail logic must be deterministic and structured — not a vibe check.

### Task 6.1 — Design the Classification Pydantic Output Schema

```python
# app/services/classifier_service.py
from pydantic import BaseModel, Field
from enum import Enum

class ClassificationResult(BaseModel):
    classification: str = Field(
        description="Must be exactly 'SAFE_TO_FIX' or 'BACKEND_BUG'"
    )
    reasoning: str = Field(
        description="Step-by-step explanation referencing the 3-point checklist"
    )
    is_happy_path: bool = Field(description="Is this test a success/happy-path scenario?")
    is_security_rule: bool = Field(description="Does this test verify auth, rate-limiting, or a security constraint?")
    is_response_type_changed: bool = Field(description="Did the response type change from success to error or vice versa?")
```

Using a structured output parser forces the LLM to explicitly populate each guardrail boolean before reaching a conclusion.

### Task 6.2 — Implement PyGithub File Fetcher

```python
# app/services/github_service.py
from github import Github
from app.config import settings

def get_github_client() -> Github:
    return Github(settings.github_pat)

def fetch_java_file(test_class_path: str) -> str:
    """
    Fetches raw Java source from GitHub.
    test_class_path example: "src/test/java/com/example/OrderApiTest.java"
    """
    g = get_github_client()
    repo = g.get_repo(f"{settings.github_repo_owner}/{settings.github_repo_name}")
    
    try:
        file_content = repo.get_contents(test_class_path, ref=settings.github_default_branch)
        return file_content.decoded_content.decode("utf-8")
    except Exception as e:
        raise FileNotFoundError(f"Could not fetch {test_class_path} from GitHub: {e}")
```

**Note on `test_class_path`:** The parser extracts a class name from the ExtentReport. The GitHub service needs to know the full path including `src/test/java/`. Either store the full path in the report, or build a path-resolution helper that searches the repo tree:

```python
def resolve_file_path(class_name: str) -> str:
    """Search repo tree for the .java file matching class_name."""
    g = get_github_client()
    repo = g.get_repo(f"{settings.github_repo_owner}/{settings.github_repo_name}")
    tree = repo.get_git_tree(settings.github_default_branch, recursive=True)
    filename = class_name.split("/")[-1]  # e.g. "OrderApiTest.java"
    matches = [item.path for item in tree.tree if item.path.endswith(filename)]
    if not matches:
        raise FileNotFoundError(f"No file found for {class_name}")
    return matches[0]  # take first match; warn if multiple
```

### Task 6.3 — Build the Classification LangChain Chain

```python
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain.output_parsers import PydanticOutputParser
from langchain.prompts import ChatPromptTemplate

def build_classification_chain():
    llm = ChatGoogleGenerativeAI(
        model=settings.llm_model_name,
        google_api_key=settings.llm_api_key,
        temperature=0  # deterministic for guardrails
    )
    parser = PydanticOutputParser(pydantic_object=ClassificationResult)
    
    prompt = ChatPromptTemplate.from_messages([
        ("system", """You are a senior QA engineer analyzing API test failures.
        
Your ONLY job is to classify whether a test failure represents a safe test update (SAFE_TO_FIX)
or a genuine backend regression (BACKEND_BUG).

Apply this MANDATORY 3-point checklist. If ANY answer is YES, classify as BACKEND_BUG:
  1. Is this a happy-path/success scenario test? (e.g., test expects HTTP 200 or a successful response body)
  2. Does this test verify a security rule? (e.g., authentication, rate limiting, authorization)
  3. Did the response type change completely? (e.g., was expecting success but got an error, or vice versa)

SAFE_TO_FIX criteria (ALL must be true):
  - The test is a NEGATIVE/ERROR scenario (expects an error code or error message)
  - ONLY the text of the error message or the specific status code changed
  - The response is still an error type (not a success response)

{format_instructions}"""),
        ("human", """Analyze this test failure:

Test Name: {test_name}
Assertion Error: {assertion_error}
Actual API Response: {actual_response}
CURL Command: {curl_command}

Java Test Source Code:
```java
{java_source}
```

Apply the 3-point checklist and provide your structured classification.""")
    ])
    
    return prompt | llm | parser
```

### Task 6.4 — Implement `task_classify_failures` Huey Task

```python
@huey.task()
def task_classify_failures(job_id: str):
    import asyncio
    
    async def _run():
        chain = build_classification_chain()
        failures = await failures_collection.find({"job_id": job_id}).to_list(None)
        
        for failure in failures:
            try:
                java_source = fetch_java_file(
                    resolve_file_path(failure["test_class_path"])
                )
                result: ClassificationResult = chain.invoke({
                    "test_name": failure["test_name"],
                    "assertion_error": failure["assertion_error"] or "",
                    "actual_response": failure["actual_response"] or "",
                    "curl_command": failure["curl_command"] or "",
                    "java_source": java_source,
                    "format_instructions": parser.get_format_instructions()
                })
                
                await failures_collection.update_one(
                    {"id": failure["id"]},
                    {"$set": {
                        "classification": result.classification,
                        "llm_reasoning": result.reasoning
                    }}
                )
            except Exception as e:
                # Don't fail the whole job for one failure — mark it UNCLASSIFIED
                await failures_collection.update_one(
                    {"id": failure["id"]},
                    {"$set": {
                        "classification": "UNCLASSIFIED",
                        "llm_reasoning": f"Classification failed: {str(e)}"
                    }}
                )
        
        await jobs_collection.update_one(
            {"id": job_id},
            {"$set": {"status": JobStatus.AWAITING_USER_APPROVAL,
                       "updated_at": datetime.utcnow()}}
        )
    
    asyncio.run(_run())
```

---

## Phase 7: Human-in-the-Loop Review UI (Phase 3 of Workflow)

### Task 7.1 — Build Job Detail Router

```python
# GET /jobs/{job_id}
@router.get("/jobs/{job_id}")
async def job_detail(job_id: str, request: Request, current_user=Depends(get_current_user)):
    job = await jobs_collection.find_one({"id": job_id, "user_id": current_user.id})
    if not job:
        raise HTTPException(404)
    
    failures = await failures_collection.find({"job_id": job_id}).to_list(None)
    
    return templates.TemplateResponse("job_detail.html", {
        "request": request,
        "job": job,
        "failures": failures,
        "JobStatus": JobStatus,
        "Classification": Classification
    })
```

### Task 7.2 — Build `job_detail.html` Jinja2 Template

Key UI components:

```html
<!-- Status badge with color coding -->
{% set badge_color = {
  'PARSING': 'secondary',
  'PENDING_CLASSIFICATION': 'info',
  'AWAITING_USER_APPROVAL': 'warning',
  'EXECUTING_FIXES': 'primary',
  'COMPLETED': 'success',
  'FAILED': 'danger'
} %}
<span class="badge bg-{{ badge_color[job.status] }}">{{ job.status }}</span>

<!-- Failures table — only show checkboxes for SAFE_TO_FIX items -->
{% for failure in failures %}
<tr class="{{ 'table-success' if failure.classification == 'SAFE_TO_FIX' 
              else 'table-danger' if failure.classification == 'BACKEND_BUG' 
              else 'table-warning' }}">
  <td>
    {% if failure.classification == 'SAFE_TO_FIX' and job.status == 'AWAITING_USER_APPROVAL' %}
    <input type="checkbox" name="approved_ids" value="{{ failure.id }}" checked>
    {% endif %}
  </td>
  <td>{{ failure.test_name }}</td>
  <td><span class="badge">{{ failure.classification }}</span></td>
  <td>
    <button class="btn btn-sm btn-outline-info" 
            data-bs-toggle="collapse" 
            data-bs-target="#reasoning-{{ loop.index }}">
      View Reasoning
    </button>
    <div id="reasoning-{{ loop.index }}" class="collapse mt-2">
      <pre class="bg-light p-2">{{ failure.llm_reasoning }}</pre>
    </div>
  </td>
  <td><code>{{ failure.assertion_error }}</code></td>
</tr>
{% endfor %}

<!-- Execute Fixes form — only shown when AWAITING_USER_APPROVAL -->
{% if job.status == 'AWAITING_USER_APPROVAL' %}
<form method="POST" action="/jobs/{{ job.id }}/execute">
  <!-- checkboxes rendered above feed into this form -->
  <button type="submit" class="btn btn-primary">Execute Fixes & Create PR</button>
</form>
{% endif %}

<!-- PR link when COMPLETED -->
{% if job.status == 'COMPLETED' and job.github_pr_url %}
<a href="{{ job.github_pr_url }}" target="_blank" class="btn btn-success">
  View GitHub Pull Request
</a>
{% endif %}
```

### Task 7.3 — Build Execute Endpoint

```python
# POST /jobs/{job_id}/execute
@router.post("/jobs/{job_id}/execute")
async def execute_fixes(
    job_id: str,
    approved_ids: list[str] = Form(...),
    current_user=Depends(get_current_user)
):
    # Validate job ownership and status
    job = await jobs_collection.find_one({
        "id": job_id, 
        "user_id": current_user.id,
        "status": JobStatus.AWAITING_USER_APPROVAL
    })
    if not job:
        raise HTTPException(400, "Job not in approval state")
    
    # Mark approved failures
    await failures_collection.update_many(
        {"id": {"$in": approved_ids}},
        {"$set": {"user_approved": True}}
    )
    
    # Update job status
    await jobs_collection.update_one(
        {"id": job_id},
        {"$set": {"status": JobStatus.EXECUTING_FIXES, "updated_at": datetime.utcnow()}}
    )
    
    # Dispatch final Huey task
    task_execute_fixes(job_id, approved_ids)
    
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)
```

---

## Phase 8: Code Fixing & GitHub PR Creation (Phase 4 of Workflow)

This phase uses a **hybrid approach**: PyGithub API for repository operations (branch creation, PR creation), and GitPython for local file mutation.

### Task 8.1 — Implement the Code Fixer LangChain Chain

```python
# app/services/fixer_service.py
from langchain.prompts import ChatPromptTemplate

class FixedTestFile(BaseModel):
    fixed_java_source: str = Field(description="The complete, corrected Java source file")
    changes_description: str = Field(description="Bullet list of what was changed and why")

def build_fixer_chain():
    llm = ChatGoogleGenerativeAI(model=settings.llm_model_name, temperature=0)
    parser = PydanticOutputParser(pydantic_object=FixedTestFile)
    
    prompt = ChatPromptTemplate.from_messages([
        ("system", """You are a QA engineer fixing API test assertions.
        
STRICT RULES:
- ONLY change assertion values (expected strings, status codes, response body matchers)
- Do NOT change test logic, method signatures, imports, or test structure
- Do NOT add or remove test cases
- The fix must make the actual response become the new expected value
- Return the COMPLETE file contents, not a diff

{format_instructions}"""),
        ("human", """Fix the failing assertion in this test file.

Test Name: {test_name}
Old Assertion Error (what failed): {assertion_error}
Actual API Response (the new correct value): {actual_response}

Current Java Source:
```java
{java_source}
```

Update only the assertion that caused the failure.""")
    ])
    
    return prompt | llm | parser
```

### Task 8.2 — Implement GitHub Operations in `github_service.py`

This is the most detailed step. Here's the complete PyGithub + GitPython workflow:

```python
# app/services/github_service.py
import os, shutil
from github import Github, GithubException
from git import Repo  # GitPython
from app.config import settings

def create_fix_branch_name(job_id: str) -> str:
    return f"test-fix-job-{job_id}"

def clone_repository(local_path: str) -> Repo:
    """Clone repo using PAT-authenticated URL."""
    auth_url = (
        f"https://{settings.github_pat}@github.com/"
        f"{settings.github_repo_owner}/{settings.github_repo_name}.git"
    )
    if os.path.exists(local_path):
        shutil.rmtree(local_path)  # fresh clone every time
    return Repo.clone_from(auth_url, local_path, branch=settings.github_default_branch)

def create_remote_branch(branch_name: str) -> None:
    """Use PyGithub to create the branch ref on the remote before pushing."""
    g = Github(settings.github_pat)
    repo = g.get_repo(f"{settings.github_repo_owner}/{settings.github_repo_name}")
    
    # Get the SHA of the default branch HEAD
    source_branch = repo.get_branch(settings.github_default_branch)
    source_sha = source_branch.commit.sha
    
    try:
        repo.create_git_ref(
            ref=f"refs/heads/{branch_name}",
            sha=source_sha
        )
    except GithubException as e:
        if e.status == 422:  # Branch already exists
            pass  # Safe to continue for idempotency
        else:
            raise

def write_fixed_file(local_repo_path: str, file_path: str, new_content: str) -> None:
    """Write fixed Java content to the local clone."""
    full_path = os.path.join(local_repo_path, file_path)
    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    with open(full_path, "w", encoding="utf-8") as f:
        f.write(new_content)

def commit_and_push_fixes(
    local_repo: Repo,
    branch_name: str,
    changed_files: list[str],
    job_id: str
) -> None:
    """Stage, commit, and push all changed files."""
    # Checkout the new branch locally
    local_repo.git.checkout("-b", branch_name)
    
    # Stage only the changed files (not everything)
    local_repo.index.add(changed_files)
    
    local_repo.index.commit(
        f"fix(tests): automated assertion fixes for job {job_id}\n\n"
        f"Generated by Agentic Test Suite Maintenance System\n"
        f"Files modified: {', '.join(changed_files)}"
    )
    
    # Push to remote (branch was pre-created via PyGithub)
    origin = local_repo.remote("origin")
    origin.push(refspec=f"{branch_name}:{branch_name}")

def create_pull_request(job_id: str, branch_name: str, pr_body: str) -> str:
    """Create PR via PyGithub and return the PR URL."""
    g = Github(settings.github_pat)
    repo = g.get_repo(f"{settings.github_repo_owner}/{settings.github_repo_name}")
    
    pr = repo.create_pull(
        title=f"Automated Test Fixes for Job {job_id}",
        body=pr_body,
        head=branch_name,
        base=settings.github_default_branch
    )
    
    # Add labels if they exist in the repo
    try:
        pr.add_to_labels("automated", "test-fix")
    except GithubException:
        pass  # Labels may not exist; non-fatal
    
    return pr.html_url
```

### Task 8.3 — Build PR Body Generator

```python
def generate_pr_body(job_id: str, fixed_failures: list[dict]) -> str:
    lines = [
        f"## Automated Test Fixes — Job `{job_id}`",
        "",
        "This PR was generated by the Agentic Test Suite Maintenance System.",
        "All changes have been reviewed and approved by a QA engineer.",
        "",
        "### Changes Made",
        "",
        "| Test Name | File | Change Summary |",
        "|-----------|------|----------------|",
    ]
    for f in fixed_failures:
        lines.append(
            f"| `{f['test_name']}` | `{f['test_class_path']}` | {f.get('changes_description', 'Assertion updated')} |"
        )
    lines += [
        "",
        "### Guardrail Classification",
        "All modified tests were classified as `SAFE_TO_FIX` by the LLM classifier,",
        "meaning they are negative/error scenarios where only the expected error value changed.",
        "",
        "> ⚠️ Tests classified as `BACKEND_BUG` were excluded and require manual investigation."
    ]
    return "\n".join(lines)
```

### Task 8.4 — Implement `task_execute_fixes` Huey Task

```python
@huey.task()
def task_execute_fixes(job_id: str, approved_failure_ids: list[str]):
    import asyncio
    
    async def _run():
        local_repo_path = os.path.join(settings.repos_dir, f"job_{job_id}")
        branch_name = create_fix_branch_name(job_id)
        
        try:
            # 1. Fetch approved failures from MongoDB
            failures = await failures_collection.find(
                {"id": {"$in": approved_failure_ids}, "user_approved": True}
            ).to_list(None)
            
            # 2. Clone the repo locally via GitPython
            local_repo = clone_repository(local_repo_path)
            
            # 3. Create branch on remote via PyGithub
            create_remote_branch(branch_name)
            
            # 4. For each approved failure: fetch source, fix, write locally
            fixer_chain = build_fixer_chain()
            fixed_failures = []
            changed_files = []
            
            for failure in failures:
                resolved_path = resolve_file_path(failure["test_class_path"])
                java_source = fetch_java_file(resolved_path)  # from GitHub API
                
                fix_result: FixedTestFile = fixer_chain.invoke({
                    "test_name": failure["test_name"],
                    "assertion_error": failure["assertion_error"],
                    "actual_response": failure["actual_response"],
                    "java_source": java_source,
                    "format_instructions": fixer_parser.get_format_instructions()
                })
                
                # Write fixed content to local clone
                write_fixed_file(local_repo_path, resolved_path, fix_result.fixed_java_source)
                changed_files.append(resolved_path)
                
                fixed_failures.append({
                    **failure,
                    "changes_description": fix_result.changes_description
                })
            
            # 5. Commit and push all changes in one commit
            commit_and_push_fixes(local_repo, branch_name, changed_files, job_id)
            
            # 6. Create Pull Request via PyGithub
            pr_body = generate_pr_body(job_id, fixed_failures)
            pr_url = create_pull_request(job_id, branch_name, pr_body)
            
            # 7. Update Job as COMPLETED with PR URL
            await jobs_collection.update_one(
                {"id": job_id},
                {"$set": {
                    "status": JobStatus.COMPLETED,
                    "github_pr_url": pr_url,
                    "updated_at": datetime.utcnow()
                }}
            )
            
        except Exception as e:
            await jobs_collection.update_one(
                {"id": job_id},
                {"$set": {"status": JobStatus.FAILED, "error_message": str(e)}}
            )
        finally:
            # Always clean up local clone to avoid disk bloat
            if os.path.exists(local_repo_path):
                shutil.rmtree(local_repo_path)
    
    asyncio.run(_run())
```

---

## Phase 9: Wiring, Testing & Hardening

### Task 9.1 — Register All Routers in `main.py`

```python
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from contextlib import asynccontextmanager
from app.routers import auth, jobs, dashboard
from app.database import users_collection, jobs_collection, failures_collection

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create indexes on startup
    await users_collection.create_index("username", unique=True)
    await jobs_collection.create_index([("user_id", 1), ("status", 1)])
    await failures_collection.create_index("job_id")
    yield

app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(auth.router)
app.include_router(jobs.router)
app.include_router(dashboard.router)
```

### Task 9.2 — Integration Test: Parser

Write an offline script `scripts/test_parser.py` that:
1. Takes a real ExtentReport HTML file as input
2. Runs `parse_extent_report()` directly (no Huey, no HTTP)
3. Prints extracted failures as JSON
4. Validates that `test_name`, `assertion_error`, `actual_response` are non-null

Adjust BeautifulSoup selectors in `parser_service.py` until all fields parse correctly.

### Task 9.3 — Integration Test: Classifier

Write `scripts/test_classifier.py` that:
1. Loads a known `SAFE_TO_FIX` and a known `BACKEND_BUG` test failure
2. Runs classification chain directly
3. Asserts that each is classified correctly
4. Prints guardrail boolean breakdown (`is_happy_path`, `is_security_rule`, `is_response_type_changed`)

### Task 9.4 — Integration Test: GitHub Operations

Write `scripts/test_github.py` that:
1. Calls `fetch_java_file()` on a known file path — verify content returned
2. Calls `resolve_file_path()` on a class name — verify path resolved
3. Creates a test branch `test-github-integration-{timestamp}`, pushes a dummy commit, creates a draft PR, then deletes the branch — full round-trip validation

### Task 9.5 — End-to-End Happy Path Test

Manually execute the full flow:
1. Start MongoDB (`docker-compose up`)
2. Start Uvicorn (`uvicorn app.main:app --reload`)
3. Start Huey (`huey_consumer app.tasks.huey_tasks.huey`)
4. Register a user via `/register`
5. Login, upload a known ExtentReport
6. Watch status progress through all stages in the dashboard (10s refresh)
7. Approve one `SAFE_TO_FIX` failure in the review UI
8. Click Execute Fixes, verify PR appears in GitHub

### Task 9.6 — Error Path Hardening

Ensure the following failure scenarios are handled gracefully:

| Scenario | Expected Behavior |
|---|---|
| ExtentReport has zero failures | Job set to FAILED with clear message |
| LLM API is rate-limited | Retry with exponential backoff (use `@huey.task(retries=3, retry_delay=30)`) |
| Java file not found in GitHub repo | Mark that `TestFailure` as UNCLASSIFIED, continue others |
| GitHub PAT lacks push permissions | Job set to FAILED with `error_message` explaining permission issue |
| Duplicate branch name (resubmitted job) | `create_remote_branch` handles 422 gracefully |
| User approves 0 failures and clicks Execute | Validate in endpoint; redirect with flash error |

---

## Phase 10: Final Polish

### Task 10.1 — Add Flash Message Support

Since Jinja2 doesn't have built-in flash messages, implement via a signed cookie:

```python
# Middleware or dependency
def set_flash(response: Response, message: str, category: str = "info"):
    response.set_cookie("flash_msg", f"{category}:{message}", max_age=5)

# In template base.html
{% if request.cookies.get('flash_msg') %}
  {% set parts = request.cookies['flash_msg'].split(':', 1) %}
  <div class="alert alert-{{ parts[0] }}">{{ parts[1] }}</div>
{% endif %}
```

### Task 10.2 — Add Job Deletion / Cleanup Endpoint

`DELETE /jobs/{job_id}` — removes job, associated failures, local report file, and local repo clone if present.

### Task 10.3 — Write `README.md`

Document:
- Prerequisites (Python 3.11+, MongoDB, GitHub PAT scopes required: `repo`, `pull_requests`)
- `.env` setup
- Two-terminal startup commands
- Known limitations of the POC (no WebSockets, single-user PAT, SQLite broker limits)

---

## Execution Sequence Summary

```
Phase 0  → Scaffold, deps, config, DB client
Phase 1  → Pydantic data models (User, Job, TestFailure)
Phase 2  → Auth system (JWT cookies, login/logout templates)
Phase 3  → Huey setup + task stubs
Phase 4  → Upload endpoint + dashboard template
Phase 5  → HTML parser + task_parse_report
Phase 6  → Classifier chain + GitHub file fetcher + task_classify_failures
Phase 7  → Human review UI + execute endpoint
Phase 8  → Fixer chain + full GitHub PR workflow + task_execute_fixes
Phase 9  → Integration tests at each layer + E2E happy path
Phase 10 → Flash messages, cleanup, documentation
```

Each phase builds directly on the previous. The critical path items — the LLM guardrail logic (Phase 6) and the GitHub hybrid PR workflow (Phase 8) — are deliberately sequenced after their dependencies (data models, auth, parsing) are stable, so integration testing can happen in isolation before the full pipeline is wired.
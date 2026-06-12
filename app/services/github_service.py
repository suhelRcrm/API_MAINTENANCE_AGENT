"""
GitHub operations using a hybrid approach:
- PyGithub  → file fetch, branch ref creation, PR creation
- GitPython → local clone, file mutation, commit, push

This hybrid avoids the GitHub API's complex multi-file commit requirement
while still using the cleaner API surface for branch/PR management.
"""
import os
import shutil
import stat
import time
from typing import Optional

from github import Github, GithubException
from git import Repo
from git.exc import GitCommandError

from app.config import settings
from app.logger import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Client helpers
# ---------------------------------------------------------------------------

def _get_github_repo():
    g = Github(settings.github_pat)
    return g.get_repo(f"{settings.github_repo_owner}/{settings.github_repo_name}")


# ---------------------------------------------------------------------------
# File fetching (pre-clone, used during classification)
# ---------------------------------------------------------------------------

def fetch_java_file(file_path: str) -> tuple[str, str, bool]:
    """
    Fetch raw file content from GitHub via API.

    Returns (content_str, original_line_ending, original_had_trailing_newline).
    Callers must pass original_line_ending and original_had_trailing_newline
    to write_fixed_file() so the round-trip is byte-for-byte identical on
    untouched lines.

    file_path must be the full repo-relative path, e.g.
    'src/test/java/com/example/OrderApiTest.java'
    """
    repo = _get_github_repo()
    try:
        file_obj = repo.get_contents(file_path, ref=settings.github_default_branch)
        raw_bytes: bytes = file_obj.decoded_content

        original_ending = "\r\n" if b"\r\n" in raw_bytes else "\n"
        original_had_trailing_newline = raw_bytes.endswith(b"\n")

        content_str = raw_bytes.decode("utf-8")
        return content_str, original_ending, original_had_trailing_newline
    except GithubException as e:
        raise FileNotFoundError(
            f"Could not fetch '{file_path}' from GitHub "
            f"({settings.github_repo_owner}/{settings.github_repo_name}): {e}"
        )


def resolve_file_path(class_path: str) -> str:
    """
    Resolve a class path like 'com/example/OrderApiTest.java' to the full
    repo-relative path by searching the repository tree.

    Returns the first match. If multiple matches exist a warning is printed
    and the first result is used (deterministic; adjust if needed).
    """
    # If class_path already looks like a full path, try it directly first
    if class_path.startswith("src/"):
        try:
            fetch_java_file(class_path)
            return class_path
        except FileNotFoundError:
            pass

    java_file = class_path.split("/")[-1]  # e.g. "OrderApiTest.java"

    if not java_file or java_file == ".java":
        raise FileNotFoundError(
            f"Cannot resolve file path: class_path is empty or invalid ('{class_path}'). "
            "The parser did not extract a class name for this test failure."
        )

    # Reject obvious non-test sources (parser should not emit these after the fix)
    _LIBRARY_JAVA = {
        "assert.java", "matcherassert.java", "jsonpath.java",
        "directconstructorhandleaccessor.java", "method.java",
    }
    if java_file.lower() in _LIBRARY_JAVA:
        raise FileNotFoundError(
            f"Resolved filename '{java_file}' is a library/JDK class, not a project test. "
            "Re-parse the report or check Script failed at method in the Extent HTML."
        )

    repo = _get_github_repo()
    tree = repo.get_git_tree(settings.github_default_branch, recursive=True)

    matches = [item.path for item in tree.tree if item.path.endswith(java_file)]

    if not matches:
        raise FileNotFoundError(
            f"No file matching '{java_file}' found in "
            f"{settings.github_repo_owner}/{settings.github_repo_name}"
        )

    # Prefer test source trees over main or unrelated paths
    def _match_rank(path: str) -> tuple[int, int]:
        p = path.replace("\\", "/").lower()
        if "/src/test/java/" in p:
            tier = 0
        elif "/test/" in p and p.endswith(".java"):
            tier = 1
        elif "/src/main/java/" in p:
            tier = 3
        else:
            tier = 2
        return (tier, len(p))

    matches.sort(key=_match_rank)
    if len(matches) > 1:
        log.warning(
            "Multiple file matches, using best-ranked path",
            extra={"java_file": java_file, "chosen": matches[0], "all": matches[:5]},
        )

    return matches[0]


# ---------------------------------------------------------------------------
# Branch name helper
# ---------------------------------------------------------------------------

def create_fix_branch_name(job_id: str) -> str:
    return f"test-fix-job-{job_id}"


# ---------------------------------------------------------------------------
# Local repo operations (GitPython)
# ---------------------------------------------------------------------------

def _rmtree_onerror(func, path, _exc_info):
    """Clear read-only flags on Windows so locked .git pack files can be removed."""
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        pass


def _release_git_locks(local_path: str) -> None:
    """Close an existing repo handle and remove stale Git lock files."""
    if not os.path.isdir(local_path):
        return
    try:
        repo = Repo(local_path)
        repo.close()
    except Exception:
        pass
    for lock_name in ("index.lock", "HEAD.lock", "shallow.lock"):
        lock_path = os.path.join(local_path, ".git", lock_name)
        if os.path.isfile(lock_path):
            try:
                os.chmod(lock_path, stat.S_IWRITE)
                os.unlink(lock_path)
            except OSError:
                pass


def remove_local_repo(local_path: str, max_retries: int = 5, retry_delay: float = 0.5) -> None:
    """
    Delete a prior clone directory. Retries on Windows when .git pack files
    are still locked by GitPython or a crashed worker.
    """
    if not os.path.exists(local_path):
        return

    _release_git_locks(local_path)
    last_err: Optional[Exception] = None

    for attempt in range(max_retries):
        try:
            shutil.rmtree(local_path, onerror=_rmtree_onerror)
            return
        except PermissionError as exc:
            last_err = exc
            _release_git_locks(local_path)
            if attempt < max_retries - 1:
                wait = retry_delay * (attempt + 1)
                log.warning(
                    "Repo directory locked — retrying removal",
                    extra={"path": local_path, "attempt": attempt + 1, "wait_seconds": wait},
                )
                time.sleep(wait)

    if last_err:
        raise last_err


def _is_retryable_git_clone_error(exc: BaseException) -> bool:
    """Network/SSL/transfer errors that often succeed on retry."""
    msg = f"{type(exc).__name__}: {exc}".lower()
    signals = (
        "rpc failed",
        "ssl_read",
        "ssl routines",
        "decryption failed",
        "bad record mac",
        "early eof",
        "unexpected disconnect",
        "invalid index-pack",
        "curl 56",
        "connection reset",
        "connection aborted",
        "timed out",
        "timeout",
        "could not read",
        "failed to connect",
        "exit code(128)",
        "exit code 128",
    )
    return any(s in msg for s in signals)


def _clone_multi_options() -> list[str]:
    """Git CLI options to improve clone reliability on large repos."""
    opts = ["-c", "http.postBuffer=524288000"]
    if settings.github_clone_depth > 0:
        opts.extend(["--depth", str(settings.github_clone_depth)])
    return opts


def clone_repository(local_path: str) -> Repo:
    """
    Clone repo using PAT-authenticated HTTPS URL.
    Retries transient network/SSL failures and removes partial clones between attempts.
    """
    auth_url = (
        f"https://{settings.github_pat}@github.com/"
        f"{settings.github_repo_owner}/{settings.github_repo_name}.git"
    )
    max_retries = settings.git_clone_max_retries
    delay = settings.git_clone_retry_delay_seconds
    last_exc: Optional[BaseException] = None

    for attempt in range(max_retries):
        try:
            remove_local_repo(local_path)
            log.info(
                "Cloning repository",
                extra={
                    "path": local_path,
                    "branch": settings.github_default_branch,
                    "attempt": attempt + 1,
                    "max_retries": max_retries,
                },
            )
            return Repo.clone_from(
                auth_url,
                local_path,
                branch=settings.github_default_branch,
                allow_unsafe_options=True,
                multi_options=_clone_multi_options(),
            )
        except Exception as exc:
            last_exc = exc
            log.warning(
                "Git clone attempt failed",
                extra={
                    "path": local_path,
                    "attempt": attempt + 1,
                    "error": str(exc)[:500],
                },
            )
            try:
                remove_local_repo(local_path)
            except Exception as cleanup_exc:
                log.warning(
                    "Could not remove partial clone after failed attempt",
                    extra={"path": local_path, "error": str(cleanup_exc)},
                )

            if attempt < max_retries - 1 and _is_retryable_git_clone_error(exc):
                wait = delay * (attempt + 1)
                log.warning(
                    "Retrying git clone after transient error",
                    extra={"wait_seconds": wait, "next_attempt": attempt + 2},
                )
                time.sleep(wait)
                continue
            break

    if last_exc:
        raise RuntimeError(
            f"Git clone failed after {max_retries} attempt(s) "
            f"({settings.github_repo_owner}/{settings.github_repo_name} "
            f"branch={settings.github_default_branch}): {last_exc}"
        ) from last_exc
    raise RuntimeError(f"Git clone failed for {local_path}")


def write_fixed_file(
    local_repo_path: str,
    file_path: str,
    content: str,
    original_ending: str = "\n",
    original_had_trailing_newline: bool = True,
) -> None:
    """
    Write patched content back to disk with the SAME line endings and trailing-
    newline state as the original file, so Git only sees the changed lines.

    `content` is expected to use LF line endings (normalised during patching).
    """
    full_path = os.path.join(local_repo_path, file_path)
    os.makedirs(os.path.dirname(full_path), exist_ok=True)

    if original_ending == "\r\n":
        content = content.replace("\n", "\r\n")

    if original_had_trailing_newline and not content.endswith(original_ending):
        content += original_ending
    elif not original_had_trailing_newline and content.endswith(original_ending):
        content = content.rstrip(original_ending)

    log.debug(
        "write_fixed_file line-ending check",
        extra={
            "path": file_path,
            "ending": repr(original_ending),
            "trailing_newline": original_had_trailing_newline,
            "crlf_in_content": content.count("\r\n"),
            "content_length": len(content),
        },
    )

    with open(full_path, "w", encoding="utf-8", newline="") as f:
        f.write(content)


def commit_and_push_fixes(
    local_repo: Repo,
    branch_name: str,
    changed_files: list[str],
    job_id: str,
) -> None:
    """Create a local branch, stage changed files, commit, and push."""
    local_repo.git.checkout("-b", branch_name)
    local_repo.index.add(changed_files)
    local_repo.index.commit(
        f"fix(tests): automated assertion fixes for job {job_id}\n\n"
        f"Generated by Agentic Test Suite Maintenance System.\n"
        f"Files modified: {', '.join(changed_files)}"
    )
    origin = local_repo.remote("origin")
    origin.push(refspec=f"{branch_name}:{branch_name}")


# ---------------------------------------------------------------------------
# Remote branch creation (PyGithub)
# ---------------------------------------------------------------------------

def create_remote_branch(branch_name: str) -> None:
    """Create the branch ref on the remote before pushing."""
    repo = _get_github_repo()
    source_branch = repo.get_branch(settings.github_default_branch)
    source_sha = source_branch.commit.sha
    try:
        repo.create_git_ref(ref=f"refs/heads/{branch_name}", sha=source_sha)
        log.info("Remote branch created", extra={"branch": branch_name})
    except GithubException as e:
        if e.status == 422:
            log.warning("Branch already exists, continuing", extra={"branch": branch_name})
        else:
            raise


# ---------------------------------------------------------------------------
# PR creation (PyGithub)
# ---------------------------------------------------------------------------

def generate_pr_body(job_id: str, fixed_failures: list[dict]) -> str:
    java_fixes = [f for f in fixed_failures if f.get("fix_type", "java") == "java"]
    schema_fixes = [f for f in fixed_failures if f.get("fix_type") == "schema"]

    lines = [
        f"## Automated Test Fixes — Job `{job_id}`",
        "",
        "This PR was generated by the **Agentic Test Suite Maintenance System**.",
        f"**{len(fixed_failures)} test(s) fixed** — "
        f"{len(java_fixes)} Java assertion change(s), "
        f"{len(schema_fixes)} JSON schema update(s).",
        "",
        "---",
        "",
    ]

    if java_fixes:
        lines += [
            f"### Java Assertion Changes ({len(java_fixes)} test(s))",
            "",
            "| # | Test Name | File | Change Summary |",
            "|---|-----------|------|----------------|",
        ]
        for i, f in enumerate(java_fixes, 1):
            file_short = f.get("test_class_path", "").split("/")[-1]
            desc = f.get("changes_description", "Assertion updated")
            lines.append(
                f"| {i} | `{f['test_name']}` "
                f"| `{file_short}` "
                f"| {desc} |"
            )
        lines.append("")

    if schema_fixes:
        lines += [
            f"### JSON Schema Updates ({len(schema_fixes)} test(s))",
            "",
            "| # | Test Name | Schema File | Change Summary |",
            "|---|-----------|-------------|----------------|",
        ]
        for i, f in enumerate(schema_fixes, 1):
            file_short = f.get("test_class_path", "").split("/")[-1]
            desc = f.get("changes_description", "JSON schema type updated")
            lines.append(
                f"| {i} | `{f['test_name']}` "
                f"| `{file_short}` "
                f"| {desc} |"
            )
        lines.append("")

    lines += [
        "---",
        "",
        "### Guardrail Classification",
        "",
        "All modified tests were classified as `SAFE_TO_FIX` by the LLM classifier.",
        "Changes are limited to assertion values and JSON schema type definitions.",
        "",
        "> ⚠️ **Reviewer**: Please verify each change reflects the intentional API behaviour before merging.",
        "> Tests classified as `BACKEND_BUG` were excluded and require manual investigation.",
    ]
    return "\n".join(lines)


def create_pull_request(job_id: str, branch_name: str, pr_body: str) -> str:
    """Create a PR via PyGithub and return the PR HTML URL."""
    repo = _get_github_repo()
    pr = repo.create_pull(
        title=f"Automated Test Fixes for Job {job_id}",
        body=pr_body,
        head=branch_name,
        base=settings.github_default_branch,
    )
    try:
        pr.add_to_labels("automated", "test-fix")
    except GithubException:
        log.warning("Could not add labels to PR (labels may not exist in repo)", extra={"pr_url": pr.html_url})
    log.info("Pull request created", extra={"pr_url": pr.html_url, "job_id": job_id})
    return pr.html_url

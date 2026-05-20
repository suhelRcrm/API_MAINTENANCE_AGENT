"""
Huey task definitions.

All tasks are synchronous (Huey requirement). Motor (async) calls inside tasks
are executed via asyncio.run(). Because each asyncio.run() creates a fresh event
loop, each task creates its own Motor client — Motor clients are bound to the
event loop they were created in and cannot be shared across loops.

Task structure contract:
  1. Create Motor client FIRST (before any other imports or logic)
  2. Wrap ALL logic in try/except so the job is always set to FAILED on error
  3. Close the Motor client in finally

Run the worker:
    huey_consumer app.tasks.huey_tasks.huey
"""
import asyncio
import os
import shutil
import stat
from datetime import datetime

from huey import SqliteHuey
from motor.motor_asyncio import AsyncIOMotorClient

from app.config import settings
from app.logger import get_logger
from app.models.job import JobStatus
from app.models.test_failure import Classification

log = get_logger(__name__)
huey = SqliteHuey(filename="huey_storage.db")


def _make_collections():
    """Create a fresh Motor client + collection handles for one asyncio.run() call."""
    client = AsyncIOMotorClient(settings.mongodb_url)
    db = client[settings.db_name]
    return client, db["jobs"], db["test_failures"]


def _run_async(coro):
    return asyncio.run(coro)


async def _mark_failed(jobs_col, job_id: str, stage: str, exc: Exception):
    """Write FAILED status to MongoDB. Best-effort — swallows its own errors."""
    try:
        await jobs_col.update_one(
            {"id": job_id},
            {"$set": {
                "status": JobStatus.FAILED,
                "error_message": f"[{stage}] {type(exc).__name__}: {exc}",
                "updated_at": datetime.utcnow(),
            }},
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Task 1 — Parse HTML report
# ---------------------------------------------------------------------------

@huey.task(retries=2, retry_delay=10)
def task_parse_report(job_id: str):
    async def _run():
        log.info("Task started", extra={"job_id": job_id})
        client, jobs_col, failures_col = _make_collections()
        try:
            from app.services.parser_service import parse_extent_report

            job = await jobs_col.find_one({"id": job_id})
            if not job:
                log.warning("Job not found in DB, skipping", extra={"job_id": job_id})
                return

            failures = parse_extent_report(job["report_file_path"], job_id)
            if not failures:
                raise ValueError("No failed tests found in the uploaded report.")

            await failures_col.insert_many([f.dict() for f in failures])
            await jobs_col.update_one(
                {"id": job_id},
                {"$set": {
                    "status": JobStatus.PENDING_CLASSIFICATION,
                    "updated_at": datetime.utcnow(),
                }},
            )
            log.info("Parsed and chaining to classification", extra={"job_id": job_id, "failures": len(failures)})
            task_classify_failures(job_id)

        except Exception as exc:
            log.error("Parse task failed", extra={"job_id": job_id, "error": str(exc)}, exc_info=True)
            await _mark_failed(jobs_col, job_id, "parse", exc)
        finally:
            client.close()

    _run_async(_run())


# ---------------------------------------------------------------------------
# Task 2 — Classify failures via LLM
# ---------------------------------------------------------------------------

@huey.task(retries=3, retry_delay=30)
def task_classify_failures(job_id: str):
    async def _run():
        log.info("Task started", extra={"job_id": job_id})
        client, jobs_col, failures_col = _make_collections()
        try:
            from app.services.classifier_service import classify_batch

            failures = await failures_col.find({"job_id": job_id}).to_list(None)
            log.info("Classifying failures (batch)", extra={"job_id": job_id, "count": len(failures)})

            # Single LLM call for the entire batch
            batch_results = classify_batch(failures)

            # Build lookup: test_case name → result dict
            result_map: dict[str, dict] = {r["test_case"]: r for r in batch_results}

            for failure in failures:
                test_name = failure.get("test_name", "")
                result = result_map.get(test_name)

                if result is None:
                    log.warning(
                        "LLM did not return result for failure, marking UNCLASSIFIED",
                        extra={"job_id": job_id, "test": test_name},
                    )
                    classification = Classification.UNCLASSIFIED
                    reasoning = "Not returned by LLM batch response."
                else:
                    raw_type = result.get("type", "UNCLASSIFIED").upper().strip()
                    try:
                        classification = Classification(raw_type)
                    except ValueError:
                        classification = Classification.UNCLASSIFIED
                    reasoning = result.get("reason", "")

                await failures_col.update_one(
                    {"id": failure["id"]},
                    {"$set": {
                        "classification": classification,
                        "llm_reasoning": reasoning,
                    }},
                )

            # Collect all SAFE_TO_FIX failure IDs — agent fixes them automatically
            safe_failures = await failures_col.find(
                {"job_id": job_id, "classification": Classification.SAFE_TO_FIX}
            ).to_list(None)
            safe_ids = [f["id"] for f in safe_failures]

            bug_count = await failures_col.count_documents(
                {"job_id": job_id, "classification": Classification.BACKEND_BUG}
            )

            log.info(
                "Classification complete",
                extra={"job_id": job_id, "safe_to_fix": len(safe_ids), "backend_bugs": bug_count},
            )

            if not safe_ids:
                # Nothing to fix — mark job complete with no PR
                await jobs_col.update_one(
                    {"id": job_id},
                    {"$set": {
                        "status": JobStatus.COMPLETED,
                        "updated_at": datetime.utcnow(),
                    }},
                )
                log.info("No SAFE_TO_FIX failures found, job complete with no PR", extra={"job_id": job_id})
                return

            # Mark all safe failures as agent-approved
            await failures_col.update_many(
                {"id": {"$in": safe_ids}},
                {"$set": {"user_approved": True}},
            )

            await jobs_col.update_one(
                {"id": job_id},
                {"$set": {
                    "status": JobStatus.EXECUTING_FIXES,
                    "updated_at": datetime.utcnow(),
                }},
            )
            # Auto-dispatch — no human approval step
            task_execute_fixes(job_id, safe_ids)

        except Exception as exc:
            log.error("Classify task failed", extra={"job_id": job_id, "error": str(exc)}, exc_info=True)
            await _mark_failed(jobs_col, job_id, "classify", exc)
        finally:
            client.close()

    _run_async(_run())


# ---------------------------------------------------------------------------
# Task 3 — Execute fixes and create GitHub PR
# ---------------------------------------------------------------------------

@huey.task(retries=1, retry_delay=15)
def task_execute_fixes(job_id: str, approved_failure_ids: list):
    async def _run():
        log.info("Task started", extra={"job_id": job_id, "approved_count": len(approved_failure_ids)})
        client, jobs_col, failures_col = _make_collections()
        local_repo_path = os.path.join(settings.repos_dir, f"job_{job_id}")
        local_repo = None  # track so we can close before rmtree

        try:
            from app.services.fixer_service import fix_test_file
            from app.services.github_service import (
                clone_repository,
                create_remote_branch,
                fetch_java_file,
                resolve_file_path,
                write_fixed_file,
                commit_and_push_fixes,
                create_pull_request,
                generate_pr_body,
                create_fix_branch_name,
            )

            branch_name = create_fix_branch_name(job_id)

            failures = await failures_col.find(
                {"id": {"$in": approved_failure_ids}, "user_approved": True}
            ).to_list(None)

            if not failures:
                raise ValueError("No approved failures found to fix.")

            local_repo = clone_repository(local_repo_path)
            create_remote_branch(branch_name)

            fixed_failures = []
            changed_files = []

            for failure in failures:
                resolved_path = resolve_file_path(failure["test_class_path"])
                java_source = fetch_java_file(resolved_path)

                fix_result = fix_test_file(failure, java_source)

                if fix_result["replacements_applied"] == 0:
                    log.info(
                        "No replacements for failure, skipping file write",
                        extra={"job_id": job_id, "test": failure.get("test_name")},
                    )
                    continue

                write_fixed_file(local_repo_path, resolved_path, fix_result["patched_source"])
                changed_files.append(resolved_path)
                fixed_failures.append({
                    **failure,
                    "changes_description": fix_result["changes_summary"],
                })
                log.info(
                    "Fix applied",
                    extra={
                        "job_id": job_id,
                        "test": failure.get("test_name"),
                        "replacements": fix_result["replacements_applied"],
                    },
                )

            commit_and_push_fixes(local_repo, branch_name, changed_files, job_id)

            pr_body = generate_pr_body(job_id, fixed_failures)
            pr_url = create_pull_request(job_id, branch_name, pr_body)

            await jobs_col.update_one(
                {"id": job_id},
                {"$set": {
                    "status": JobStatus.COMPLETED,
                    "github_pr_url": pr_url,
                    "updated_at": datetime.utcnow(),
                }},
            )
            log.info("Execute task complete", extra={"job_id": job_id, "pr_url": pr_url, "files_changed": len(changed_files)})

        except Exception as exc:
            log.error("Execute task failed", extra={"job_id": job_id, "error": str(exc)}, exc_info=True)
            await _mark_failed(jobs_col, job_id, "execute", exc)
        finally:
            client.close()
            # Close GitPython repo to release Windows file locks before cleanup
            if local_repo is not None:
                try:
                    local_repo.close()
                except Exception:
                    pass
            if os.path.exists(local_repo_path):
                def _force_remove(func, path, _):
                    try:
                        os.chmod(path, stat.S_IWRITE)
                        func(path)
                    except Exception:
                        pass
                shutil.rmtree(local_repo_path, onerror=_force_remove)

    _run_async(_run())

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
import time
from datetime import datetime
from typing import Optional

from huey import SqliteHuey
from motor.motor_asyncio import AsyncIOMotorClient

from app.config import settings
from app.logger import get_logger
from app.models.job import JobFailedStage, JobStatus
from app.models.test_failure import Classification, FixStatus

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
    """Write FAILED status and record which pipeline stage failed."""
    try:
        await jobs_col.update_one(
            {"id": job_id},
            {"$set": {
                "status": JobStatus.FAILED,
                "last_failed_stage": stage,
                "error_message": f"[{stage}] {type(exc).__name__}: {exc}",
                "updated_at": datetime.utcnow(),
            }},
        )
    except Exception:
        pass


from app.services.job_pipeline import is_failure_classified


async def _set_fix_status(
    failures_col,
    failure_id: str,
    status: FixStatus,
    reason: Optional[str] = None,
):
    await failures_col.update_one(
        {"id": failure_id},
        {"$set": {
            "fix_status": status,
            "fix_reason": reason,
        }},
    )


async def _after_classification(jobs_col, failures_col, job_id: str):
    """Post-classify: complete job or chain to fix execution."""
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
        await jobs_col.update_one(
            {"id": job_id},
            {"$set": {
                "status": JobStatus.COMPLETED,
                "updated_at": datetime.utcnow(),
            }},
        )
        log.info("No SAFE_TO_FIX failures found, job complete with no PR", extra={"job_id": job_id})
        return

    await failures_col.update_many(
        {"id": {"$in": safe_ids}},
        {"$set": {
            "user_approved": True,
            "fix_status": FixStatus.PENDING,
            "fix_reason": None,
        }},
    )

    await jobs_col.update_one(
        {"id": job_id},
        {"$set": {
            "status": JobStatus.EXECUTING_FIXES,
            "updated_at": datetime.utcnow(),
        }},
    )
    task_execute_fixes(job_id, safe_ids)


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

def _parse_classification_result(result: dict | None, test_name: str) -> tuple[Classification, str]:
    if result is None:
        return Classification.UNCLASSIFIED, "Not returned by LLM batch response."
    raw_type = result.get("type", "UNCLASSIFIED").upper().strip()
    try:
        classification = Classification(raw_type)
    except ValueError:
        classification = Classification.UNCLASSIFIED
    return classification, result.get("reason", "")


async def _persist_classification(failures_col, failure: dict, result: dict | None, job_id: str):
    test_name = failure.get("test_name", "")
    classification, reasoning = _parse_classification_result(result, test_name)
    if result is None:
        log.warning(
            "No classification result for failure",
            extra={"job_id": job_id, "test": test_name},
        )
    await failures_col.update_one(
        {"id": failure["id"]},
        {"$set": {
            "classification": classification,
            "llm_reasoning": reasoning,
        }},
    )


@huey.task(retries=3, retry_delay=30)
def task_classify_failures(job_id: str):
    async def _run():
        log.info("Task started", extra={"job_id": job_id})
        client, jobs_col, failures_col = _make_collections()
        try:
            from app.services.classifier_service import classify_batch

            failures = await failures_col.find({"job_id": job_id}).to_list(None)
            if not failures:
                raise ValueError("No failures found for job — run parse first.")

            unclassified = [f for f in failures if not is_failure_classified(f)]
            already_classified = len(failures) - len(unclassified)

            if not unclassified:
                log.info(
                    "All failures already classified — skipping classification",
                    extra={"job_id": job_id, "count": len(failures)},
                )
                await _after_classification(jobs_col, failures_col, job_id)
                return

            log.info(
                "Classifying failures",
                extra={
                    "job_id": job_id,
                    "total": len(failures),
                    "already_classified": already_classified,
                    "to_classify": len(unclassified),
                },
            )

            llm_pending = list(unclassified)
            batch_size = settings.classify_batch_size
            num_chunks = (len(llm_pending) + batch_size - 1) // batch_size if llm_pending else 0
            log.info(
                "Classification chunks",
                extra={
                    "job_id": job_id,
                    "llm_chunks": num_chunks,
                    "llm_pending": len(llm_pending),
                },
            )

            for chunk_idx in range(num_chunks):
                start = chunk_idx * batch_size
                chunk = llm_pending[start : start + batch_size]
                log.info(
                    "LLM classification chunk",
                    extra={
                        "job_id": job_id,
                        "chunk": chunk_idx + 1,
                        "total_chunks": num_chunks,
                        "chunk_size": len(chunk),
                    },
                )
                chunk_results = classify_batch(chunk)
                result_map = {r["test_case"]: r for r in chunk_results}
                for failure in chunk:
                    await _persist_classification(
                        failures_col,
                        failure,
                        result_map.get(failure.get("test_name", "")),
                        job_id,
                    )
                if chunk_idx < num_chunks - 1:
                    time.sleep(settings.classify_batch_delay_seconds)

            await _after_classification(jobs_col, failures_col, job_id)

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
            from app.services.fixer_service import (
                apply_json_schema_update,
                normalise_line_endings,
                process_java_file_patches,
            )
            from app.services.parser_service import extract_class_path_from_script_failed_text
            from app.services.github_service import (
                clone_repository,
                create_remote_branch,
                fetch_java_file,
                resolve_file_path,
                remove_local_repo,
                write_fixed_file,
                commit_and_push_fixes,
                create_pull_request,
                generate_pr_body,
                create_fix_branch_name,
            )

            branch_name = create_fix_branch_name(job_id)

            failures = await failures_col.find(
                {"id": {"$in": approved_failure_ids}}
            ).to_list(None)

            if not failures:
                raise ValueError("No SAFE_TO_FIX failures found to fix.")

            try:
                local_repo = clone_repository(local_repo_path)
            except Exception as clone_exc:
                log.error(
                    "Repository clone failed",
                    extra={"job_id": job_id, "path": local_repo_path, "error": str(clone_exc)},
                    exc_info=True,
                )
                raise RuntimeError(
                    f"Could not clone {settings.github_repo_owner}/{settings.github_repo_name} "
                    f"(branch={settings.github_default_branch}). "
                    f"Check network/VPN and retry the job. Cause: {clone_exc}"
                ) from clone_exc

            try:
                create_remote_branch(branch_name)
            except Exception as branch_exc:
                log.error(
                    "Remote branch creation failed",
                    extra={"job_id": job_id, "branch": branch_name, "error": str(branch_exc)},
                    exc_info=True,
                )
                raise

            fixed_failures = []
            changed_files = []
            fixed_test_ids: set[str] = set()

            # ---------------------------------------------------------------
            # PHASE 1 — Java file fixes
            # Group failures by resolved Java file path so we:
            #   1. Fetch each file from GitHub exactly ONCE
            #   2. Apply all fixes sequentially on accumulated source
            #   3. Write the file to disk exactly ONCE with all patches combined
            # ---------------------------------------------------------------
            # schema_patches collects Strategy-B updates keyed by schema file path
            schema_patches: dict[str, dict] = {}

            file_patches: dict[str, dict] = {}
            for failure in failures:
                class_path = failure.get("test_class_path") or ""
                fallback = extract_class_path_from_script_failed_text(
                    failure.get("assertion_error") or ""
                )
                if fallback:
                    class_path = fallback

                try:
                    resolved_path = resolve_file_path(class_path)
                except FileNotFoundError as exc:
                    log.warning(
                        "Cannot resolve file path — skipping",
                        extra={"job_id": job_id, "test": failure.get("test_name"), "error": str(exc)},
                    )
                    await _set_fix_status(
                        failures_col,
                        failure["id"],
                        FixStatus.SKIPPED,
                        f"Could not resolve test file: {exc}",
                    )
                    continue

                if resolved_path not in file_patches:
                    java_source, original_ending, original_had_trailing_newline = fetch_java_file(
                        resolved_path
                    )
                    file_patches[resolved_path] = {
                        "source": normalise_line_endings(java_source),
                        "original_ending": original_ending,
                        "original_had_trailing_newline": original_had_trailing_newline,
                        "failures": [],
                    }
                file_patches[resolved_path]["failures"].append(failure)

            log.info(
                "Fixing Java files (parallel)",
                extra={
                    "job_id": job_id,
                    "file_count": len(file_patches),
                    "fixer_max_workers": settings.fixer_max_workers,
                    "fixer_llm_parallel_per_file": settings.fixer_llm_parallel_per_file,
                },
            )

            patch_results: list = []
            if not file_patches:
                log.warning(
                    "No test files resolved for fixing — check test_class_path / Script failed at method",
                    extra={"job_id": job_id, "approved": len(failures)},
                )
            else:
                patch_results = process_java_file_patches(file_patches)

            for resolved_path, accumulated_source, file_had_fix, outcomes in patch_results:
                for failure, fix_result, fix_exc in outcomes:
                    if fix_exc is not None:
                        log.warning(
                            "LLM fixer raised — skipping this failure, continuing others",
                            extra={
                                "job_id": job_id,
                                "test": failure.get("test_name"),
                                "file": resolved_path,
                                "error": str(fix_exc),
                            },
                        )
                        await _set_fix_status(
                            failures_col,
                            failure["id"],
                            FixStatus.FAILED,
                            str(fix_exc),
                        )
                        continue
                    if fix_result is None:
                        await _set_fix_status(
                            failures_col,
                            failure["id"],
                            FixStatus.SKIPPED,
                            "Fixer returned no result.",
                        )
                        continue

                    if not fix_result.get("parse_ok", True):
                        await _set_fix_status(
                            failures_col,
                            failure["id"],
                            FixStatus.FAILED,
                            fix_result.get("changes_summary", "LLM response parse error."),
                        )
                        continue

                    for su in fix_result.get("schema_updates", []):
                        sf = su.get("schema_file", "").strip()
                        if sf:
                            if sf not in schema_patches:
                                schema_patches[sf] = {"updates": [], "test_items": []}
                            schema_patches[sf]["updates"].append({
                                **su,
                                "_failure_id": str(failure["id"]),
                            })
                            schema_patches[sf]["test_items"].append({
                                "failure": failure,
                                "summary": fix_result.get("changes_summary", "Schema type updated"),
                            })

                    if fix_result["replacements_applied"] == 0:
                        if not fix_result.get("schema_updates"):
                            log.info(
                                "No replacements and no schema updates — skipping",
                                extra={"job_id": job_id, "test": failure.get("test_name")},
                            )
                            await _set_fix_status(
                                failures_col,
                                failure["id"],
                                FixStatus.SKIPPED,
                                fix_result.get("changes_summary")
                                or "No applicable Java or schema changes produced.",
                            )
                        continue

                    await _set_fix_status(
                        failures_col,
                        failure["id"],
                        FixStatus.FIXED,
                        fix_result.get("changes_summary"),
                    )
                    fixed_failures.append({
                        **failure,
                        "changes_description": fix_result["changes_summary"],
                        "fix_type": "java",
                    })
                    fixed_test_ids.add(str(failure["id"]))
                    log.info(
                        "Java fix applied",
                        extra={
                            "job_id": job_id,
                            "test": failure.get("test_name"),
                            "replacements": fix_result["replacements_applied"],
                            "file": resolved_path,
                        },
                    )

                if file_had_fix:
                    patch_meta = file_patches[resolved_path]
                    write_fixed_file(
                        local_repo_path,
                        resolved_path,
                        accumulated_source,
                        original_ending=patch_meta["original_ending"],
                        original_had_trailing_newline=patch_meta["original_had_trailing_newline"],
                    )
                    if resolved_path not in changed_files:
                        changed_files.append(resolved_path)

            # ---------------------------------------------------------------
            # PHASE 2 — JSON schema file updates (Strategy B)
            # For each unique schema file: fetch once, apply all type changes,
            # write to disk, register fixed tests in fixed_failures.
            # ---------------------------------------------------------------
            for schema_file, sp_data in schema_patches.items():
                schema_source, schema_ending, schema_trailing = fetch_java_file(schema_file)
                schema_result = apply_json_schema_update(
                    normalise_line_endings(schema_source),
                    sp_data["updates"],
                )
                applied_paths = set(schema_result["applied_paths"])

                if schema_result["content"]:
                    write_fixed_file(
                        local_repo_path,
                        schema_file,
                        schema_result["content"],
                        original_ending=schema_ending,
                        original_had_trailing_newline=schema_trailing,
                    )
                    if schema_file not in changed_files:
                        changed_files.append(schema_file)
                    log.info(
                        "JSON schema file updated",
                        extra={
                            "job_id": job_id,
                            "schema_file": schema_file,
                            "applied": len(applied_paths),
                            "skipped": len(schema_result["skipped_paths"]),
                        },
                    )

                for item in sp_data["test_items"]:
                    failure = item["failure"]
                    fid = str(failure["id"])
                    if fid in fixed_test_ids:
                        continue

                    test_paths = [
                        u.get("field_json_path", "").strip()
                        for u in sp_data["updates"]
                        if u.get("_failure_id") == fid
                    ]
                    test_paths = [p for p in test_paths if p]
                    applied_for_test = [p for p in test_paths if p in applied_paths]

                    if applied_for_test:
                        await _set_fix_status(
                            failures_col,
                            failure["id"],
                            FixStatus.FIXED,
                            item["summary"],
                        )
                        fixed_failures.append({
                            **failure,
                            "changes_description": item["summary"],
                            "fix_type": "schema",
                        })
                        fixed_test_ids.add(fid)
                        log.info(
                            "Schema fix registered",
                            extra={
                                "job_id": job_id,
                                "test": failure.get("test_name"),
                                "schema_file": schema_file,
                                "paths": applied_for_test,
                            },
                        )
                    elif test_paths:
                        skipped = [
                            s["path"]
                            for s in schema_result["skipped_paths"]
                            if s["path"] in test_paths
                        ]
                        reason = (
                            f"Schema paths not found or not applied: {', '.join(skipped or test_paths)}"
                        )
                        await _set_fix_status(
                            failures_col,
                            failure["id"],
                            FixStatus.FAILED,
                            reason,
                        )
                        log.warning(
                            "Schema fix failed for test",
                            extra={
                                "job_id": job_id,
                                "test": failure.get("test_name"),
                                "schema_file": schema_file,
                                "reason": reason,
                            },
                        )
                    else:
                        await _set_fix_status(
                            failures_col,
                            failure["id"],
                            FixStatus.SKIPPED,
                            "No schema field paths in fix proposal.",
                        )

            for failure in failures:
                fid = str(failure["id"])
                if fid not in fixed_test_ids:
                    doc = await failures_col.find_one({"id": failure["id"]}, {"fix_status": 1})
                    if doc and doc.get("fix_status") in (
                        FixStatus.PENDING,
                        FixStatus.PENDING.value,
                    ):
                        await _set_fix_status(
                            failures_col,
                            failure["id"],
                            FixStatus.SKIPPED,
                            "Fix step completed but no patch was applied for this test.",
                        )

            if changed_files:
                commit_and_push_fixes(local_repo, branch_name, changed_files, job_id)
                pr_body = generate_pr_body(job_id, fixed_failures)
                pr_url = create_pull_request(job_id, branch_name, pr_body)
            else:
                pr_url = None
                log.warning(
                    "Execute finished with no file changes",
                    extra={"job_id": job_id, "approved": len(failures)},
                )

            await jobs_col.update_one(
                {"id": job_id},
                {"$set": {
                    "status": JobStatus.COMPLETED,
                    "github_pr_url": pr_url,
                    "updated_at": datetime.utcnow(),
                }},
            )
            log.info(
                "Execute task complete",
                extra={"job_id": job_id, "pr_url": pr_url, "files_changed": len(changed_files)},
            )

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
            try:
                remove_local_repo(local_repo_path)
            except Exception as cleanup_exc:
                log.warning(
                    "Could not remove local repo clone",
                    extra={"job_id": job_id, "path": local_repo_path, "error": str(cleanup_exc)},
                )

    _run_async(_run())

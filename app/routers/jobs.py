import os
import shutil
from datetime import datetime

from fastapi import APIRouter, Request, UploadFile, File, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from bson import ObjectId

from app.dependencies import get_current_user
from app.models.job import Job, JobFailedStage, JobStatus
from app.models.test_failure import Classification, FixStatus
from app.config import settings
from app.services.job_pipeline import all_failures_classified, count_unclassified
import app.database as db_module

router = APIRouter(prefix="/jobs", tags=["jobs"])
templates = Jinja2Templates(directory="app/templates")


@router.post("/upload")
async def upload_report(
    request: Request,
    file: UploadFile = File(...),
    current_user=Depends(get_current_user),
):
    if not file.filename or not file.filename.lower().endswith(".html"):
        jobs = await db_module.jobs_collection \
            .find({}) \
            .sort("created_at", -1) \
            .to_list(100)
        user_ids = list({j["user_id"] for j in jobs if j.get("user_id")})
        users = await db_module.users_collection.find(
            {"id": {"$in": user_ids}}, {"id": 1, "username": 1}
        ).to_list(None)
        user_map = {u["id"]: u["username"] for u in users}
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "current_user": current_user,
                "jobs": jobs,
                "user_map": user_map,
                "upload_error": "Only .html files are accepted.",
            },
            status_code=400,
        )

    job_id = str(ObjectId())
    os.makedirs(settings.reports_dir, exist_ok=True)
    file_path = os.path.join(settings.reports_dir, f"{job_id}_report.html")

    with open(file_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    job = Job(id=job_id, user_id=current_user["id"], report_file_path=file_path)
    await db_module.jobs_collection.insert_one(job.dict())

    from app.tasks.huey_tasks import task_parse_report
    task_parse_report(job_id)

    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)


@router.get("/{job_id}", response_class=HTMLResponse)
async def job_detail(job_id: str, request: Request, current_user=Depends(get_current_user)):
    job = await db_module.jobs_collection.find_one({"id": job_id})
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    failures = await db_module.failures_collection.find(
        {"job_id": job_id}
    ).to_list(None)

    uploader = await db_module.users_collection.find_one(
        {"id": job.get("user_id")}, {"username": 1}
    )
    uploaded_by = uploader["username"] if uploader else job.get("user_id", "—")

    return templates.TemplateResponse(request, "job_detail.html", {
        "current_user": current_user,
        "job": job,
        "uploaded_by": uploaded_by,
        "failures": failures,
        "JobStatus": JobStatus,
        "JobFailedStage": JobFailedStage,
        "Classification": Classification,
        "FixStatus": FixStatus,
    })



@router.post("/{job_id}/retry")
async def retry_job(job_id: str, current_user=Depends(get_current_user)):
    job = await db_module.jobs_collection.find_one({"id": job_id})
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.get("status") != JobStatus.FAILED:
        response = RedirectResponse(url=f"/jobs/{job_id}", status_code=303)
        response.set_cookie(
            "flash_msg",
            "warning:Only FAILED jobs can be retried.",
            max_age=5,
        )
        return response

    error_msg = job.get("error_message", "") or ""
    failed_stage = job.get("last_failed_stage")

    # Prefer explicit stage; fall back for jobs created before last_failed_stage existed
    if not failed_stage:
        if "execute:" in error_msg:
            failed_stage = JobFailedStage.EXECUTE
        elif "classify:" in error_msg:
            failed_stage = JobFailedStage.CLASSIFY
        else:
            failed_stage = JobFailedStage.PARSE

    clear_fields = {
        "error_message": None,
        "last_failed_stage": None,
        "updated_at": datetime.utcnow(),
    }

    if failed_stage == JobFailedStage.EXECUTE or failed_stage == JobFailedStage.EXECUTE.value:
        # Parse + classify already done — re-run fixes only
        safe_failures = await db_module.failures_collection.find(
            {"job_id": job_id, "classification": Classification.SAFE_TO_FIX}
        ).to_list(None)
        safe_ids = [f["id"] for f in safe_failures]

        if not safe_ids:
            response = RedirectResponse(url=f"/jobs/{job_id}", status_code=303)
            response.set_cookie(
                "flash_msg",
                "warning:No SAFE_TO_FIX failures found to re-execute.",
                max_age=5,
            )
            return response

        await db_module.jobs_collection.update_one(
            {"id": job_id},
            {"$set": {**clear_fields, "status": JobStatus.EXECUTING_FIXES}},
        )
        from app.tasks.huey_tasks import task_execute_fixes
        task_execute_fixes(job_id, safe_ids)
        flash = "info:Re-running fix execution (skipping parse & classify)."

    elif failed_stage == JobFailedStage.CLASSIFY or failed_stage == JobFailedStage.CLASSIFY.value:
        failures_col = db_module.failures_collection
        if await all_failures_classified(failures_col, job_id):
            safe_failures = await failures_col.find(
                {"job_id": job_id, "classification": Classification.SAFE_TO_FIX}
            ).to_list(None)
            safe_ids = [f["id"] for f in safe_failures]
            if safe_ids:
                await failures_col.update_many(
                    {"id": {"$in": safe_ids}},
                    {"$set": {
                        "user_approved": True,
                        "fix_status": FixStatus.PENDING,
                        "fix_reason": None,
                    }},
                )
                await db_module.jobs_collection.update_one(
                    {"id": job_id},
                    {"$set": {**clear_fields, "status": JobStatus.EXECUTING_FIXES}},
                )
                from app.tasks.huey_tasks import task_execute_fixes
                task_execute_fixes(job_id, safe_ids)
                flash = (
                    "info:Classification already saved in DB — resuming fix execution "
                    "(no LLM re-classification)."
                )
            else:
                await db_module.jobs_collection.update_one(
                    {"id": job_id},
                    {"$set": {**clear_fields, "status": JobStatus.COMPLETED}},
                )
                flash = (
                    "info:All failures already classified in DB (none SAFE_TO_FIX). "
                    "Job marked complete."
                )
        else:
            remaining = await count_unclassified(failures_col, job_id)
            await db_module.jobs_collection.update_one(
                {"id": job_id},
                {"$set": {**clear_fields, "status": JobStatus.PENDING_CLASSIFICATION}},
            )
            from app.tasks.huey_tasks import task_classify_failures
            task_classify_failures(job_id)
            flash = (
                f"info:Re-running classification for {remaining} unclassified failure(s) "
                "(already-labeled rows are skipped)."
            )

    else:
        # Parse failed or unknown — full restart
        await db_module.jobs_collection.update_one(
            {"id": job_id},
            {"$set": {
                **clear_fields,
                "status": JobStatus.PARSING,
                "github_pr_url": None,
            }},
        )
        await db_module.failures_collection.delete_many({"job_id": job_id})
        from app.tasks.huey_tasks import task_parse_report
        task_parse_report(job_id)
        flash = "info:Job re-queued from the beginning."

    response = RedirectResponse(url=f"/jobs/{job_id}", status_code=303)
    response.set_cookie("flash_msg", flash, max_age=5)
    return response


@router.delete("/{job_id}")
@router.post("/{job_id}/delete")
async def delete_job(job_id: str, current_user=Depends(get_current_user)):
    job = await db_module.jobs_collection.find_one(
        {"id": job_id}
    )
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    report_path = job.get("report_file_path")
    if report_path and os.path.exists(report_path):
        os.remove(report_path)

    local_repo_path = os.path.join(settings.repos_dir, f"job_{job_id}")
    if os.path.exists(local_repo_path):
        shutil.rmtree(local_repo_path)

    await db_module.failures_collection.delete_many({"job_id": job_id})
    await db_module.jobs_collection.delete_one({"id": job_id})

    response = RedirectResponse(url="/dashboard", status_code=303)
    response.set_cookie("flash_msg", "success:Job deleted successfully.", max_age=5)
    return response

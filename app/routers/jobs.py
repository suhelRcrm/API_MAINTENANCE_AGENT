import os
import shutil
from datetime import datetime

from fastapi import APIRouter, Request, UploadFile, File, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from bson import ObjectId

from app.dependencies import get_current_user
from app.models.job import Job, JobStatus
from app.models.test_failure import Classification
from app.config import settings
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
            .find({"user_id": current_user["id"]}) \
            .sort("created_at", -1) \
            .to_list(50)
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "current_user": current_user,
                "jobs": jobs,
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
    job = await db_module.jobs_collection.find_one(
        {"id": job_id, "user_id": current_user["id"]}
    )
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    failures = await db_module.failures_collection.find(
        {"job_id": job_id}
    ).to_list(None)

    return templates.TemplateResponse(request, "job_detail.html", {
        "current_user": current_user,
        "job": job,
        "failures": failures,
        "JobStatus": JobStatus,
        "Classification": Classification,
    })



@router.post("/{job_id}/retry")
async def retry_job(job_id: str, current_user=Depends(get_current_user)):
    job = await db_module.jobs_collection.find_one(
        {"id": job_id, "user_id": current_user["id"]}
    )
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

    # Determine which stage failed and resume from there
    if "execute:" in error_msg:
        # Parse + classify already done — re-run fixes only
        safe_failures = await db_module.failures_collection.find(
            {"job_id": job_id, "classification": "SAFE_TO_FIX", "user_approved": True}
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
            {"$set": {
                "status": JobStatus.EXECUTING_FIXES,
                "error_message": None,
                "updated_at": datetime.utcnow(),
            }},
        )
        from app.tasks.huey_tasks import task_execute_fixes
        task_execute_fixes(job_id, safe_ids)
        flash = "info:Re-running fix execution (skipping parse & classify)."

    elif "classify:" in error_msg:
        # Parse done — re-run classify only (failures already in DB)
        await db_module.jobs_collection.update_one(
            {"id": job_id},
            {"$set": {
                "status": JobStatus.PENDING_CLASSIFICATION,
                "error_message": None,
                "updated_at": datetime.utcnow(),
            }},
        )
        from app.tasks.huey_tasks import task_classify_failures
        task_classify_failures(job_id)
        flash = "info:Re-running classification (skipping parse)."

    else:
        # Parse failed or unknown — full restart
        await db_module.jobs_collection.update_one(
            {"id": job_id},
            {"$set": {
                "status": JobStatus.PARSING,
                "error_message": None,
                "github_pr_url": None,
                "updated_at": datetime.utcnow(),
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
        {"id": job_id, "user_id": current_user["id"]}
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

from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from app.dependencies import get_current_user
import app.database as db_module

router = APIRouter(tags=["dashboard"])
templates = Jinja2Templates(directory="app/templates")


@router.get("/", response_class=HTMLResponse)
async def root_redirect():
    return RedirectResponse(url="/dashboard", status_code=302)


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, current_user=Depends(get_current_user)):
    jobs = await db_module.jobs_collection.find(
        {"user_id": current_user["id"]}
    ).sort("created_at", -1).to_list(50)

    return templates.TemplateResponse(request, "dashboard.html", {
        "current_user": current_user,
        "jobs": jobs,
    })

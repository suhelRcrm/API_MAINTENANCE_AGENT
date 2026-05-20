from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from app.services.auth_service import hash_password, verify_password, create_access_token
from app.models.user import UserInDB
import app.database as db_module

router = APIRouter(tags=["auth"])
templates = Jinja2Templates(directory="app/templates")


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login")
async def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    try:
        user = await db_module.users_collection.find_one({"username": username})
        if not user or not verify_password(password, user["password_hash"]):
            return templates.TemplateResponse(
                request,
                "login.html",
                {"error": "Invalid username or password"},
                status_code=401,
            )

        token = create_access_token({"sub": username})
        response = RedirectResponse(url="/dashboard", status_code=303)
        response.set_cookie(
            key="access_token",
            value=token,
            httponly=True,
            max_age=60 * 60 * 8,
            samesite="lax",
        )
        return response

    except Exception:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "An unexpected error occurred. Please try again."},
            status_code=500,
        )


@router.post("/logout")
async def logout():
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie("access_token")
    return response


@router.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    return templates.TemplateResponse(request, "register.html", {"error": None})


@router.post("/register")
async def register(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    try:
        if len(password) < 6:
            return templates.TemplateResponse(
                request,
                "register.html",
                {"error": "Password must be at least 6 characters."},
                status_code=400,
            )

        existing = await db_module.users_collection.find_one({"username": username})
        if existing:
            return templates.TemplateResponse(
                request,
                "register.html",
                {"error": "Username already taken. Please choose another."},
                status_code=400,
            )

        user = UserInDB(username=username, password_hash=hash_password(password))
        await db_module.users_collection.insert_one(user.dict())

        response = RedirectResponse(url="/login", status_code=303)
        response.set_cookie(
            "flash_msg",
            f"success:Account created successfully! Welcome, {username}. Please sign in.",
            max_age=10,
        )
        return response

    except Exception:
        return templates.TemplateResponse(
            request,
            "register.html",
            {"error": "Registration failed due to a server error. Please try again."},
            status_code=500,
        )

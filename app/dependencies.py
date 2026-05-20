from fastapi import Request, HTTPException
from fastapi.responses import RedirectResponse
from app.services.auth_service import decode_access_token
import app.database as db_module


async def get_current_user(request: Request):
    """
    FastAPI dependency that reads the access_token cookie, validates the JWT,
    and returns the UserInDB document. Redirects to /login on failure.
    """
    token = request.cookies.get("access_token")
    if not token:
        raise HTTPException(status_code=303, headers={"Location": "/login"})

    payload = decode_access_token(token)
    if not payload:
        raise HTTPException(status_code=303, headers={"Location": "/login"})

    username = payload.get("sub")
    if not username:
        raise HTTPException(status_code=303, headers={"Location": "/login"})

    user = await db_module.users_collection.find_one({"username": username})
    if not user:
        raise HTTPException(status_code=303, headers={"Location": "/login"})

    return user

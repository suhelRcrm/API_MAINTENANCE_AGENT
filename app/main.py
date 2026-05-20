from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import app.database as db_module
from app.logger import get_logger
from app.routers import auth, jobs, dashboard

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize MongoDB connection
    client = db_module.get_client()
    db_module.init_db(client)
    db_module.setup_collections()

    # Create indexes
    await db_module.users_collection.create_index("username", unique=True)
    await db_module.jobs_collection.create_index([("user_id", 1), ("status", 1)])
    await db_module.failures_collection.create_index("job_id")
    log.info("Application startup complete", extra={"mongodb": db_module.db.name})

    yield

    client.close()
    log.info("Application shutdown complete")


app = FastAPI(
    title="API Maintenance Agent",
    description="Agentic Test Suite Maintenance System",
    version="1.0.0",
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory="app/static"), name="static")

app.include_router(auth.router)
app.include_router(jobs.router)
app.include_router(dashboard.router)

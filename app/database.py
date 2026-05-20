from motor.motor_asyncio import AsyncIOMotorClient
from app.config import settings

client: AsyncIOMotorClient = None  # type: ignore
db = None


def get_client() -> AsyncIOMotorClient:
    return AsyncIOMotorClient(settings.mongodb_url)


def init_db(mongo_client: AsyncIOMotorClient):
    global client, db
    client = mongo_client
    db = client[settings.db_name]


# Collection handles — populated after init_db() is called at startup
def get_users_collection():
    return db["users"]


def get_jobs_collection():
    return db["jobs"]


def get_failures_collection():
    return db["test_failures"]


# Module-level references set up during lifespan startup
users_collection = None
jobs_collection = None
failures_collection = None


def setup_collections():
    global users_collection, jobs_collection, failures_collection
    users_collection = db["users"]
    jobs_collection = db["jobs"]
    failures_collection = db["test_failures"]

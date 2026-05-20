from pydantic import BaseModel, Field
from bson import ObjectId
from datetime import datetime


class UserInDB(BaseModel):
    id: str = Field(default_factory=lambda: str(ObjectId()))
    username: str
    password_hash: str
    created_at: datetime = Field(default_factory=datetime.utcnow)

    class Config:
        populate_by_name = True

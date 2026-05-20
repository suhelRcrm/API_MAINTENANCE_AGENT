from pydantic import BaseModel, Field
from bson import ObjectId
from datetime import datetime
from enum import Enum
from typing import Optional


class JobStatus(str, Enum):
    PARSING = "PARSING"
    PENDING_CLASSIFICATION = "PENDING_CLASSIFICATION"
    EXECUTING_FIXES = "EXECUTING_FIXES"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class Job(BaseModel):
    id: str = Field(default_factory=lambda: str(ObjectId()))
    user_id: str
    report_file_path: str
    status: JobStatus = JobStatus.PARSING
    github_pr_url: Optional[str] = None
    error_message: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    class Config:
        populate_by_name = True
        use_enum_values = True

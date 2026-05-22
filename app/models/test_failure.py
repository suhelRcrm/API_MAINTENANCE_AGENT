from pydantic import BaseModel, Field
from bson import ObjectId
from enum import Enum
from typing import Optional


class Classification(str, Enum):
    SAFE_TO_FIX = "SAFE_TO_FIX"
    BACKEND_BUG = "BACKEND_BUG"
    UNCLASSIFIED = "UNCLASSIFIED"


class FixStatus(str, Enum):
    """Outcome of the automated fix step (SAFE_TO_FIX failures only)."""
    NOT_APPLICABLE = "NOT_APPLICABLE"
    PENDING = "PENDING"
    FIXED = "FIXED"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"


class TestFailure(BaseModel):
    id: str = Field(default_factory=lambda: str(ObjectId()))
    job_id: str
    test_name: str
    test_class_path: str
    curl_command: Optional[str] = None
    actual_response: Optional[str] = None
    assertion_error: Optional[str] = None
    classification: Classification = Classification.UNCLASSIFIED
    llm_reasoning: Optional[str] = None
    user_approved: bool = False
    fix_status: FixStatus = FixStatus.NOT_APPLICABLE
    fix_reason: Optional[str] = None

    class Config:
        populate_by_name = True
        use_enum_values = True

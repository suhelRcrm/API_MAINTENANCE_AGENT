"""
Shared job pipeline helpers (classification state, resume without re-LLM).
"""
from app.models.test_failure import Classification


def is_failure_classified(failure: dict) -> bool:
    """True when this failure has a stored label other than UNCLASSIFIED."""
    raw = failure.get("classification")
    if raw is None:
        return False
    if isinstance(raw, Classification):
        return raw != Classification.UNCLASSIFIED
    return str(raw).upper().strip() != Classification.UNCLASSIFIED.value


async def count_failures(failures_col, job_id: str) -> int:
    return await failures_col.count_documents({"job_id": job_id})


async def count_unclassified(failures_col, job_id: str) -> int:
    return await failures_col.count_documents({
        "job_id": job_id,
        "$or": [
            {"classification": Classification.UNCLASSIFIED},
            {"classification": Classification.UNCLASSIFIED.value},
            {"classification": None},
            {"classification": {"$exists": False}},
        ],
    })


async def all_failures_classified(failures_col, job_id: str) -> bool:
    total = await count_failures(failures_col, job_id)
    if total == 0:
        return False
    unclassified = await count_unclassified(failures_col, job_id)
    return unclassified == 0

"""
Batch LLM classification — all failures in a single API call.

Sends every failure in the job as a numbered JSON list to the model and
receives back a JSON array:
  [{"test_case": "...", "type": "SAFE_TO_FIX|BACKEND_BUG", "reason": "..."}]

Guardrail rules are embedded in the system prompt. The model is instructed to
return ONLY raw JSON — no markdown fences, no prose. The full raw response is
logged for traceability.
"""
import json
import logging
from typing import Any

from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.messages import AIMessage

from app.config import settings

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are a senior QA engineer classifying API test failures.

CLASSIFICATION RULES — apply in order:
  Mark as BACKEND_BUG if ANY of the following is true:
    1. The test verifies a happy-path / success scenario \
(expects HTTP 200 or a successful response body)
    2. The test verifies a security rule \
(authentication, authorization, rate-limiting)
    3. The response type changed completely \
(was expecting success but got an error, or vice versa)

  Mark as SAFE_TO_FIX only when ALL of the following are true:
    - The test is a NEGATIVE / ERROR scenario (expects an error code or error message)
    - Only the text of the error message or the specific status code value changed
    - The response is still an error type (not a success response)

OUTPUT FORMAT — return a JSON array ONLY. No markdown, no explanation, no extra text.
[
  {{"test_case": "<exact test name from input>", "type": "SAFE_TO_FIX", "reason": "<20-30 words>"}},
  {{"test_case": "<exact test name from input>", "type": "BACKEND_BUG",  "reason": "<20-30 words>"}}
]

REASON must be 20-30 words maximum. Every test_case in the input MUST appear in the output."""

_HUMAN_PROMPT = """Classify every test failure listed below. Return the JSON array only.

{failures_payload}"""


# ---------------------------------------------------------------------------
# Chain builder
# ---------------------------------------------------------------------------

def build_batch_classifier():
    llm = ChatGoogleGenerativeAI(
        model=settings.llm_model_name,
        google_api_key=settings.google_api_key,
        temperature=0,
    )
    prompt = ChatPromptTemplate.from_messages([
        ("system", _SYSTEM_PROMPT),
        ("human", _HUMAN_PROMPT),
    ])
    return prompt | llm


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def classify_batch(failures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Send all failures to the LLM in one call.

    Returns a list of dicts:
      [{"test_case": str, "type": "SAFE_TO_FIX"|"BACKEND_BUG", "reason": str}]

    Falls back to UNCLASSIFIED for any failure not present in the LLM response
    or if JSON parsing fails entirely.
    """
    if not failures:
        return []

    # Build the numbered payload sent to the model
    items = []
    for i, f in enumerate(failures, start=1):
        items.append({
            "index": i,
            "test_case": f.get("test_name", ""),
            "assertion_error": f.get("assertion_error", "") or "",
            "actual_response": (f.get("actual_response", "") or "")[:500],
            "curl_command": (f.get("curl_command", "") or "")[:300],
        })

    failures_payload = json.dumps(items, indent=2)

    chain = build_batch_classifier()
    raw: AIMessage = chain.invoke({"failures_payload": failures_payload})
    raw_text: str = raw.content if isinstance(raw, AIMessage) else str(raw)

    log.info(
        "LLM batch classification raw response",
        extra={"response": raw_text, "failure_count": len(failures)},
    )

    # Parse the JSON array from the model response
    try:
        results: list[dict] = json.loads(raw_text.strip())
    except json.JSONDecodeError:
        # Try stripping accidental markdown fences
        cleaned = raw_text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        try:
            results = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            log.error(
                "Failed to parse LLM batch response as JSON",
                extra={"error": str(exc), "raw": raw_text[:500]},
            )
            return [
                {"test_case": f.get("test_name", ""), "type": "UNCLASSIFIED", "reason": "JSON parse error"}
                for f in failures
            ]

    return results

"""
LLM classification for test failures.

Chunked LLM calls with compact JSON payload and 429 backoff.
Goal: decide whether to update the TEST SCRIPT to match the API (SAFE_TO_FIX),
not whether to change production API behavior.

Each chunk returns:
  [{"test_case": "...", "type": "SAFE_TO_FIX|BACKEND_BUG", "reason": "..."}]
"""
import json
import logging
import time
from typing import Any, Optional

from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.messages import AIMessage

from app.config import settings

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are a senior QA engineer classifying API test failures for an automated maintenance system.

MISSION — READ FIRST:
  This system maintains API TEST SCRIPTS so they stay in sync with the CURRENT API contract.
  We update tests to match the API — we do NOT change production API code to satisfy old tests.
  Default mindset: if the API response shape, field names, types, status codes, or error text
  changed in a consistent way, the test is STALE → SAFE_TO_FIX unless there is clear evidence
  of a real product regression (wrong business outcome, security breach, broken calculation).

Your job: can this failure be auto-fixed by updating the test (assertions, JSON paths, schema,
expected messages/status codes)? Or is it a genuine backend defect a developer must fix in API code?

════════════════════════════════════════════
SAFE_TO_FIX — mark SAFE_TO_FIX if the failure matches ANY of these patterns:
════════════════════════════════════════════

PATTERN A — Error message / status code text changed:
  • The test is a NEGATIVE/ERROR scenario (expects an error response, not a success)
  • Only the wording of the error message OR the specific error status code value changed
  • The response is still an error type (not a success)
  • Example: expected "Bad Request" but got "Invalid request body" with the same 400 status

PATTERN B — JSON schema field nullability changed (LOW-RISK schema evolution):
  • The test validates a JSON schema (assertion error mentions "match the given JSON schema"
    or "JSON schema validation failed")
  • The ONLY failures are fields whose type changed between null and a primitive type:
      - Was: schema expects null, API now returns a string/number/boolean (field got enriched)
      - Or: schema expects string/number, API now returns null (field became nullable)
  • No fields were ADDED or REMOVED from the response — only the type of existing fields changed
  • The affected fields are NOT security-sensitive (not auth tokens, user IDs, permissions,
    access-control flags, role fields)
  • Example: schema expected sourceadded=null but API returns sourceadded="api"
  • Example: schema expected email=string but API returns email=null for a placeholder user

PATTERN C — Additive enum / lookup list expansion (NEEDS REVIEWER ATTENTION):
  • The test calls a static lookup/reference endpoint (invoice statuses, deal stages, entity types)
  • The test asserts an exact count or upper-bound size (e.g., hasSize(lessThan(5)))
  • The API response now has MORE items because new valid enum values were added
  • All previously existing items are still present and unchanged
  • Example: expected < 5 invoice statuses, API now returns 6 (new "Cancelled" status added)
  • ⚠ Set type=SAFE_TO_FIX but include "NEEDS_REVIEW" in the reason — a human must verify
    the new enum values don't break downstream business logic

PATTERN D — JSON key renamed or repositioned in response:
  • The assertion looks for a specific JSON field by name or path
  • The field still exists in the response but its key was renamed (e.g. "message" → "msg",
    "status_code" → "statusCode", "user_id" → "userId") OR it moved to a different level
    in the JSON structure while carrying the same data
  • The VALUE of the field is unchanged — only its key name or path changed
  • The change is NOT on a security-sensitive key (not auth token, permissions, role fields)
  • Example: assertion checks $.data.status but API now returns $.status (moved up one level)
  • Example: assertion checks body("message", ...) but key is now "msg" or "error_message"

PATTERN E — Semantically equivalent response message rewording:
  • The test asserts a specific human-readable message string from the API
  • The new message conveys EXACTLY the same meaning and intent as the old one but is worded
    differently — spelling correction, grammar fix, synonym substitution, or minor rephrasing
  • The HTTP status code is UNCHANGED
  • The scenario is still a negative/error scenario (not a success message on a success path)
  • SAFE examples:
      "successful" → "successfully"  (spelling/grammar fix)
      "Unable to update" → "Failed to update"  (synonym, same failure intent)
      "Record not found" → "No record found"  (minor rephrasing, same meaning)
      "Bad Request" → "Invalid request body"  (same 400 error, more descriptive)
  • NOT safe (these are BACKEND_BUG):
      Error message → Success message  (intent flipped)
      "Unauthorized" → "Record not found"  (different error category)
      Any change on a happy-path/success test

PATTERN F — HTTP 2xx success code migration:
  • The test asserts a specific success status code (e.g., statusCode(200)) and the API
    now returns a different 2xx code — both codes indicate success
  • The response body is still a valid success response; only the code changed
  • Common safe migrations:
      200 → 201 Created  (POST/create endpoints now correctly signal resource creation)
      200 → 204 No Content  (DELETE/update endpoints now return no body on success)
      201 → 200  (downgrade to generic OK, still success)
  • NOT safe: any 2xx → 4xx/5xx transition (that is a response type flip → BACKEND_BUG)
  • ⚠ 200 → 202 Accepted requires NEEDS_REVIEW — 202 implies async processing which may
    change the test's assumption that the operation is complete synchronously

PATTERN G — Runtime exception from stale response access (NPE, CCE, IOOBE, ISE):
  • The test threw NullPointerException, ClassCastException, IndexOutOfBoundsException,
    or IllegalStateException while reading/parsing the HTTP response in test code
  • Typical causes that are SAFE_TO_FIX (sync test to API):
      - JSON key renamed or moved (test still uses "message", API returns "messages")
      - Wrong path or index after response list/object shape changed
      - Type mismatch in test expectations (Integer vs String cast) while API response is valid
      - Optional field now null/absent and test did not null-check
  • Mark SAFE_TO_FIX when HTTP status is still appropriate for the scenario (e.g. 200 on happy
    path) OR error scenario still returns an error but structure/text drift broke the test
  • Include NEEDS_REVIEW in reason if the sample response is empty and you infer from exception
    type alone
  • NOT SAFE_TO_FIX only if evidence shows the API returned wrong business data on a happy path
    (e.g. 200 but empty list when create-then-get must return the new entity)

════════════════════════════════════════════
BACKEND_BUG — genuine API/product defects (do NOT auto-fix tests):
════════════════════════════════════════════

  1. SECURITY / ACCESS CONTROL FAILURE: Auth, authorization, rate-limit, cross-account,
     RBAC, or permission tests where the API did not enforce the expected boundary
     (e.g. expected 401/403/404 but got 200 with data).

  2. RESPONSE TYPE FLIP with wrong business meaning: Happy-path test got 4xx/5xx or empty
     payload when the operation should have succeeded; OR negative test got 200 success
     when it must fail — and the response proves incorrect product behavior (not just text drift).

  3. BUSINESS LOGIC STATE MISMATCH: Multi-step workflow (create → update → assert count/value)
     and the final asserted business state is wrong (not explainable by field rename/type drift).

  4. DATA INTEGRITY / CALCULATION MISMATCH: Asserted totals, aggregates, or derived metrics are
     numerically wrong vs response (not formatting or field path issues).

  5. STRUCTURAL SCHEMA CHANGE requiring product decision: Fields ADDED/REMOVED in ways that
     break contracts and cannot be fixed by a one-line assertion tweak (large shape redesign).
     Prefer SAFE_TO_FIX for nullability-only or rename/path fixes (Patterns B, D, G).

  Do NOT mark BACKEND_BUG merely because the failure is NullPointerException, ClassCastException,
  or IndexOutOfBoundsException — those often mean the test is out of sync with the API (Pattern G).


════════════════════════════════════════════
DECISION GUIDE (apply in order, first match wins):
════════════════════════════════════════════
  Step 1:  Security/auth/cross-account/RBAC boundary failure?                        → BACKEND_BUG
  Step 2:  JSON schema failure with ONLY nullability/type on existing fields?        → SAFE_TO_FIX (B)
  Step 3:  JSON key renamed/repositioned OR NPE/CCE/IOOBE/ISE accessing response?    → SAFE_TO_FIX (D/G)
  Step 4:  Additive enum/list expansion on lookup endpoint?                          → SAFE_TO_FIX (C) + NEEDS_REVIEW
  Step 5:  Semantically equivalent error message rewording (same status category)?   → SAFE_TO_FIX (E)
  Step 6:  2xx status code migration (both success)?                                 → SAFE_TO_FIX (F)
  Step 7:  Negative scenario — only error text or error code value changed?          → SAFE_TO_FIX (A)
  Step 8:  Response type flip OR wrong business outcome on happy path?               → BACKEND_BUG
  Step 9:  Stateful workflow final state/count wrong?                                → BACKEND_BUG
  Step 10: Calculation/aggregate numerically wrong?                                  → BACKEND_BUG
  Step 11: Ambiguous with little context — lean SAFE_TO_FIX + NEEDS_REVIEW (sync test to API)
  Step 12: Clear product regression only                                               → BACKEND_BUG

OUTPUT FORMAT — return a JSON array ONLY. No markdown, no explanation, no extra text.
[
  {{"test_case": "<exact test name from input>", "type": "SAFE_TO_FIX", "reason": "<20-30 words>"}},
  {{"test_case": "<exact test name from input>", "type": "BACKEND_BUG",  "reason": "<20-30 words>"}}
]

REASON must be 20-30 words maximum. Every test_case in the input MUST appear in the output."""

_HUMAN_PROMPT = """Classify every test failure listed below. Return the JSON array only.

{failures_payload}"""


# ---------------------------------------------------------------------------
# LLM chain
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


def _build_llm_payload(failures: list[dict[str, Any]]) -> str:
    """Compact JSON payload — no curl; short response snippet when present."""
    max_resp = settings.classify_response_max_chars
    items = []
    for i, f in enumerate(failures, start=1):
        assertion = (f.get("assertion_error") or "")[:800]
        entry: dict[str, Any] = {
            "i": i,
            "test_case": f.get("test_name", ""),
            "assertion_error": assertion,
        }
        response = (f.get("actual_response") or "").strip()
        if response:
            entry["actual_response"] = response[:max_resp]
        items.append(entry)
    return json.dumps(items, separators=(",", ":"))


def _parse_llm_json_array(raw_text: str, failures: list[dict[str, Any]]) -> list[dict]:
    try:
        return json.loads(raw_text.strip())
    except json.JSONDecodeError:
        cleaned = (
            raw_text.strip()
            .removeprefix("```json")
            .removeprefix("```")
            .removesuffix("```")
            .strip()
        )
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError as exc:
            log.error(
                "Failed to parse LLM batch response as JSON",
                extra={"error": str(exc), "raw": raw_text[:500]},
            )
            return [
                {
                    "test_case": f.get("test_name", ""),
                    "type": "UNCLASSIFIED",
                    "reason": "JSON parse error",
                }
                for f in failures
            ]


def _is_rate_limit_error(exc: BaseException) -> bool:
    msg = str(exc).upper()
    return "429" in msg or "RESOURCE_EXHAUSTED" in msg


def _invoke_llm_with_retry(chain, failures_payload: str, failure_count: int) -> str:
    last_exc: Optional[BaseException] = None
    for attempt in range(settings.classify_llm_max_retries):
        try:
            raw: AIMessage = chain.invoke({"failures_payload": failures_payload})
            return raw.content if isinstance(raw, AIMessage) else str(raw)
        except Exception as exc:
            last_exc = exc
            if _is_rate_limit_error(exc) and attempt < settings.classify_llm_max_retries - 1:
                wait = settings.classify_batch_delay_seconds * (attempt + 1)
                log.warning(
                    "LLM rate limit — backing off before retry",
                    extra={
                        "attempt": attempt + 1,
                        "wait_seconds": wait,
                        "failure_count": failure_count,
                    },
                )
                time.sleep(wait)
                continue
            raise
    if last_exc:
        raise last_exc
    raise RuntimeError("LLM invoke failed with no exception")


def classify_batch(failures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Classify one chunk via a single LLM call (lean payload, 429 retry).
    """
    if not failures:
        return []

    failures_payload = _build_llm_payload(failures)
    chain = build_batch_classifier()
    raw_text = _invoke_llm_with_retry(chain, failures_payload, len(failures))

    log.info(
        "LLM batch classification response received",
        extra={"failure_count": len(failures), "response_chars": len(raw_text)},
    )

    return _parse_llm_json_array(raw_text, failures)


def classify_all(failures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Classify via LLM in configurable chunks with delay between chunks.

    Returns one result dict per input failure (same order), each with test_case, type, reason.
    """
    if not failures:
        return []

    results_by_test: dict[str, dict[str, str]] = {}
    llm_pending = list(failures)

    log.info(
        "Classification starting",
        extra={
            "total": len(failures),
            "batch_size": settings.classify_batch_size,
        },
    )

    batch_size = settings.classify_batch_size
    num_chunks = (len(llm_pending) + batch_size - 1) // batch_size if llm_pending else 0

    for chunk_idx in range(num_chunks):
        start = chunk_idx * batch_size
        chunk = llm_pending[start : start + batch_size]
        log.info(
            "LLM classification chunk",
            extra={
                "chunk": chunk_idx + 1,
                "total_chunks": num_chunks,
                "chunk_size": len(chunk),
            },
        )
        for r in classify_batch(chunk):
            results_by_test[r.get("test_case", "")] = r

        if chunk_idx < num_chunks - 1:
            time.sleep(settings.classify_batch_delay_seconds)

    out: list[dict[str, Any]] = []
    for f in failures:
        test_name = f.get("test_name", "")
        out.append(
            results_by_test.get(
                test_name,
                {
                    "test_case": test_name,
                    "type": "UNCLASSIFIED",
                    "reason": "Not returned by LLM batch response.",
                },
            )
        )
    return out

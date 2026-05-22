"""
LLM-based test assertion fixer using Google Gemini — targeted replacement approach.

Instead of returning the full rewritten file (which causes +300/-300 diffs),
the LLM returns only the exact assertion lines to change as old→new pairs.
These are applied as surgical string replacements on the original source file,
so the GitHub PR diff shows only the actual changed lines (e.g. +2/-2).

Output contract:
  replacements: list of {"old_snippet": "...", "new_snippet": "...", "description": "..."}
    - old_snippet: the exact string from the source file (must match literally)
    - new_snippet: the replacement string (only the assertion value changes)
    - description: one-line explanation used in the PR body
"""
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
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
You are a QA engineer maintaining API test assertions for an automated test suite.
MISSION: Update the TEST SCRIPT to match the current API contract — sync tests to the API,
never change API behaviour. Make the minimum possible change to the Java test file so the
test correctly reflects the current API response (paths, keys, types, messages, status codes).

════════════════════════════════════════════
FIX STRATEGY — choose based on the failure type:
════════════════════════════════════════════

STRATEGY A — Error message / status code text changed:
  Situation: The test is a negative/error scenario and only the expected error message
  text or status code number is now stale.
  Fix: Replace ONLY the stale string literal or integer that represents the old expected
  error message or status code with the new value from the actual API response.
  Example: .body("message", equalTo("Bad Request"))
        → .body("message", equalTo("Invalid request body"))

STRATEGY B — JSON schema field type / nullability changed:
  Situation: The test uses matchesJsonSchemaInClasspath("path/to/schema.json") and
  the schema validation fails because one or more EXISTING fields changed type
  (null → string/number/boolean, or string → null, or added/removed from type array).
  Fix: The JSON schema file needs updating, NOT the Java test assertion call.
  Steps:
    1. Find the matchesJsonSchemaInClasspath("classpath/relative/path.json") call in the Java source.
    2. Convert the classpath-relative path to a full repo path:
       "schemas/foo.json" → "src/test/resources/schemas/foo.json"
    3. Identify each field that needs a type change from the assertion error.
    4. Return ZERO Java replacements and populate the "schema_updates" array.
  Each schema_update entry must have:
    - "schema_file": full repo-relative path (e.g. "src/test/resources/schemas/foo.json")
    - "field_json_path": dot-notation path to the "type" key in the schema JSON
      (e.g. "properties.someField.type" or "properties.parent.properties.child.type")
    - "old_value": the current type value (string or array)
    - "new_value": the corrected type value (always prefer array form, e.g. ["string","null"])
  Do NOT change the matchesJsonSchemaInClasspath call in the Java test.

STRATEGY C — Additive enum / lookup list size assertion:
  Situation: The test asserts a collection size (hasSize, hasSize(lessThan(N)),
  equalTo(N)) against a static lookup endpoint and the list grew because new
  valid enum values were added.
  Fix: Update the size matcher to match the new actual collection size or use
  a greaterThanOrEqualTo / hasSize that accommodates the new count.
  Prefer greaterThanOrEqualTo(oldSize) over an exact hardcoded number to be
  resilient to future additions.
  Example: .body("$", hasSize(lessThan(5)))
        → .body("$", hasSize(greaterThanOrEqualTo(5)))
  Add a comment in changes_summary: "NEEDS_REVIEW: new enum value added — reviewer
  must verify <new_item_name> doesn't break downstream business logic."

STRATEGY D — JSON key renamed or repositioned:
  Situation: The assertion references a JSON path or field name that no longer exists
  because the API renamed the key (e.g. "message" → "msg") or moved it to a different
  level in the response structure.
  Fix: Update ONLY the JSON path string or field name in the assertion to match the new
  key name or path. The value being asserted remains unchanged.
  Example: .body("data.status", equalTo("active"))
        → .body("status", equalTo("active"))  (field moved up one level)
  Example: .body("message", equalTo("Done"))
        → .body("msg", equalTo("Done"))  (key renamed)

STRATEGY E — Semantically equivalent message rewording:
  Situation: The test asserts a specific human-readable message string that the API
  changed to a synonym, spelling correction, grammar fix, or minor rephrasing with
  the same intent (e.g., "Unable to update" → "Failed to update").
  Fix: Replace ONLY the old expected message string literal with the new actual message
  string from the API response. Do not change the HTTP status code assertion or any
  other part of the test.
  Example: .body("message", equalTo("Unable to update"))
        → .body("message", equalTo("Failed to update"))

STRATEGY F — HTTP 2xx success code migration:
  Situation: The test asserts statusCode(200) but the API now returns a different 2xx
  code (201, 204) while the response is still a valid success.
  Fix: Update ONLY the integer in the statusCode() assertion to the new 2xx value.
  Example: .statusCode(200)  →  .statusCode(201)
  If migrating to 204 (No Content), also check if the test accesses response body
  fields after the status assertion — if so, add NEEDS_REVIEW to changes_summary
  noting that 204 responses have no body and body assertions will also need removal.

════════════════════════════════════════════
ABSOLUTE RULES (never violate):
════════════════════════════════════════════
- ONLY change assertion VALUES — never change method names, variable names,
  imports, test logic, or structure
- Do NOT add or remove test cases or test methods
- Do NOT reformat, reindent, or change whitespace except inside the changed value
- Each old_snippet MUST be copied EXACTLY character-for-character from the Java source
- Each new_snippet must be identical to old_snippet except for the assertion change
- If the correct fix requires a schema file update (Strategy B), return zero replacements
  and document the required schema change in changes_summary

Return a JSON object ONLY — no markdown, no explanation:
{{
  "replacements": [
    {{
      "old_snippet": "<exact substring from source including enough context to be unique>",
      "new_snippet": "<same substring with only the assertion value updated>",
      "description": "<one line: what changed and why>"
    }}
  ],
  "schema_updates": [
    {{
      "schema_file": "<full repo-relative path, e.g. src/test/resources/schemas/foo.json>",
      "field_json_path": "<dot-notation path to the type key, e.g. properties.someField.type>",
      "old_value": "<current type value, e.g. null or [\\"null\\"]>",
      "new_value": ["<corrected type array, e.g. \\"string\\", \\"null\\">"]
    }}
  ],
  "changes_summary": "<bullet list of all changes made, including NEEDS_REVIEW notes if applicable>"
}}

If no change is needed at all, return:
{{"replacements": [], "schema_updates": [], "changes_summary": "<reason nothing could be changed>"}}

Only include "schema_updates" entries for Strategy B. For all other strategies use "replacements" only and leave "schema_updates" as an empty array [].
- Return STRICTLY valid JSON only. Escape newlines inside string values as \\n. Do not truncate the response.
- For schema_updates, field_json_path MUST exist in the schema file (verify against the matchesJsonSchemaInClasspath path)."""

_HUMAN_PROMPT = """Fix the failing assertion in this Java test file.

Test Name: {test_name}
Assertion Error / Schema Validation Failure: {assertion_error}
Actual API Response (current correct value): {actual_response}

Java Source:
```java
{java_source}
```

Identify the fix strategy (A through F) based on the assertion error, then return
only the JSON object with targeted replacements."""


# ---------------------------------------------------------------------------
# Chain builder
# ---------------------------------------------------------------------------

def build_fixer_chain():
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
# Whitespace drift guard
# ---------------------------------------------------------------------------

def _guard_whitespace_drift(old: str, new: str, desc: str) -> None:
    """
    Warn (never raise) if the LLM changed whitespace or structure outside the
    core assertion value. This keeps the PR diff clean but doesn't block the fix.
    """
    def _prefix_suffix(s: str, inner: str) -> tuple[str, str]:
        idx = s.find(inner)
        if idx == -1:
            return s, ""
        return s[:idx], s[idx + len(inner):]

    # Only compare the stripped value between the assertion parens
    old_stripped = old.strip()
    new_stripped = new.strip()
    # Quick heuristic: if non-value portions differ, warn
    old_outer = old_stripped[:20] + old_stripped[-20:]
    new_outer = new_stripped[:20] + new_stripped[-20:]
    if old_outer != new_outer:
        log.warning(
            "LLM may have changed surrounding whitespace/structure — fix applied "
            "but PR diff may include minor formatting noise",
            extra={"description": desc},
        )


# ---------------------------------------------------------------------------
# JSON schema patching (Strategy B)
# ---------------------------------------------------------------------------

def _path_exists_in_schema(obj: Any, path_parts: list[str]) -> bool:
    """True if dot-notation path exists in the schema JSON object."""
    if not path_parts:
        return False
    key = path_parts[0]
    if len(path_parts) == 1:
        return isinstance(obj, dict) and key in obj
    if isinstance(obj, dict) and key in obj:
        return _path_exists_in_schema(obj[key], path_parts[1:])
    return False


def _set_nested(obj: Any, path_parts: list[str], value: Any) -> bool:
    """Recursively walk dot-notation path and set the leaf key to value."""
    if not path_parts:
        return False
    key = path_parts[0]
    if len(path_parts) == 1:
        if isinstance(obj, dict) and key in obj:
            obj[key] = value
            return True
        return False
    if isinstance(obj, dict) and key in obj:
        return _set_nested(obj[key], path_parts[1:], value)
    return False


def apply_json_schema_update(schema_source: str, updates: list[dict]) -> dict[str, Any]:
    """
    Apply type updates to a JSON schema file. Skips invalid paths instead of failing
    the whole file when at least one path is valid.

    Returns:
      {
        "content": str | None,          # serialised schema if any path applied
        "applied_paths": list[str],
        "skipped_paths": list[dict],    # [{path, reason}]
      }
    """
    schema = json.loads(schema_source)
    applied: list[str] = []
    skipped: list[dict[str, str]] = []

    for update in updates:
        path = update.get("field_json_path", "").strip()
        new_val = update.get("new_value")
        if not path or new_val is None:
            log.warning(
                "Schema update entry missing path or new_value — skipping",
                extra={"update": update},
            )
            skipped.append({"path": path or "(empty)", "reason": "missing path or new_value"})
            continue

        parts = path.split(".")
        if not _path_exists_in_schema(schema, parts):
            log.warning(
                "Schema field_json_path not found in schema — skipping",
                extra={"path": path, "available_top_keys": list(schema.keys())[:10]},
            )
            skipped.append({"path": path, "reason": "path not found in schema file"})
            continue

        if _set_nested(schema, parts, new_val):
            applied.append(path)
            log.info("Schema field updated", extra={"path": path, "new_value": new_val})
        else:
            skipped.append({"path": path, "reason": "could not set value at path"})

    content = None
    if applied:
        content = json.dumps(schema, indent=2, ensure_ascii=False)

    return {
        "content": content,
        "applied_paths": applied,
        "skipped_paths": skipped,
    }


# ---------------------------------------------------------------------------
# Apply replacements
# ---------------------------------------------------------------------------

def apply_replacements(original_source: str, replacements: list[dict]) -> tuple[str, list[str]]:
    """
    Apply targeted string replacements to the original Java source.

    Handles the shared-assertion case: when two tests in the same file assert
    the same line, the first fix patches it and the second fix's old_snippet is
    already gone — but new_snippet is already present, so it's treated as done.

    Returns (patched_source, list_of_descriptions).
    Raises ValueError only if old_snippet is absent AND new_snippet is also absent.
    """
    patched = original_source
    descriptions = []

    for rep in replacements:
        old = rep.get("old_snippet", "")
        new = rep.get("new_snippet", "")
        desc = rep.get("description", "")

        if not old:
            continue

        if old not in patched:
            # If new_snippet is already present, an earlier fix in the same file
            # already changed this line — treat as successfully applied.
            if new and new in patched:
                log.info(
                    "Assertion already patched by earlier fix in same file — skipping",
                    extra={"description": desc, "snippet": old[:80]},
                )
                descriptions.append(desc)
                continue
            raise ValueError(
                f"old_snippet not found in source (and new_snippet also absent).\n"
                f"Snippet: {old[:120]!r}"
            )

        # Warn (don't block) if LLM drifted whitespace outside the changed value
        _guard_whitespace_drift(old, new, desc)

        patched = patched.replace(old, new, 1)
        descriptions.append(desc)
        log.info("Applied replacement", extra={"description": desc})

    return patched, descriptions


# ---------------------------------------------------------------------------
# LLM JSON parsing
# ---------------------------------------------------------------------------

def _strip_markdown_fence(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return cleaned


def _extract_braced_json(text: str) -> Optional[str]:
    """Take the outermost {...} block when the model adds prose around JSON."""
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return text[start : end + 1]
    return None


def _normalize_fix_proposal(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {
            "replacements": [],
            "schema_updates": [],
            "changes_summary": "LLM response was not a JSON object.",
        }
    return {
        "replacements": raw.get("replacements") if isinstance(raw.get("replacements"), list) else [],
        "schema_updates": raw.get("schema_updates") if isinstance(raw.get("schema_updates"), list) else [],
        "changes_summary": str(raw.get("changes_summary") or ""),
    }


def _parse_fixer_json(raw_text: str, test_name: str) -> tuple[dict[str, Any], bool]:
    """
    Parse fixer LLM output with fallbacks for control chars and truncated JSON.
    Returns (proposal, parse_ok).
    """
    cleaned = _strip_markdown_fence(raw_text)
    if not cleaned:
        return {
            "replacements": [],
            "schema_updates": [],
            "changes_summary": "LLM returned empty response.",
        }, False

    candidates = [cleaned]
    braced = _extract_braced_json(cleaned)
    if braced and braced != cleaned:
        candidates.append(braced)

    last_error: Optional[json.JSONDecodeError] = None
    for blob in candidates:
        for loader in (lambda s: json.loads(s, strict=False), lambda s: json.loads(s)):
            try:
                return _normalize_fix_proposal(loader(blob)), True
            except json.JSONDecodeError as exc:
                last_error = exc
                continue

    log.error(
        "Failed to parse LLM JSON after fallbacks",
        extra={
            "test": test_name,
            "error": str(last_error),
            "raw": cleaned[:400],
        },
    )
    return {
        "replacements": [],
        "schema_updates": [],
        "changes_summary": f"LLM response parse error: {last_error}",
    }, False


def _proposal_is_actionable(proposal: dict[str, Any]) -> bool:
    if proposal.get("replacements"):
        return True
    for su in proposal.get("schema_updates") or []:
        if su.get("field_json_path") and su.get("new_value") is not None:
            return True
    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _is_rate_limit_error(exc: BaseException) -> bool:
    msg = str(exc).upper()
    return "429" in msg or "RESOURCE_EXHAUSTED" in msg


def _invoke_fixer_llm(failure: dict[str, Any], java_source: str) -> dict[str, Any]:
    """
    LLM-only: return parsed fix proposal (replacements, schema_updates, changes_summary).
    Does not mutate java_source.
    """
    chain = build_fixer_chain()
    last_exc: Optional[BaseException] = None
    raw_text = ""

    for attempt in range(settings.fixer_llm_max_retries):
        try:
            raw: AIMessage = chain.invoke({
                "test_name": failure.get("test_name", ""),
                "assertion_error": failure.get("assertion_error", "") or "",
                "actual_response": failure.get("actual_response", "") or "",
                "java_source": java_source,
            })
            raw_text = raw.content if isinstance(raw, AIMessage) else str(raw)
            break
        except Exception as exc:
            last_exc = exc
            if _is_rate_limit_error(exc) and attempt < settings.fixer_llm_max_retries - 1:
                wait = settings.classify_batch_delay_seconds * (attempt + 1)
                log.warning(
                    "Fixer LLM rate limit — backing off",
                    extra={
                        "test": failure.get("test_name"),
                        "attempt": attempt + 1,
                        "wait_seconds": wait,
                    },
                )
                time.sleep(wait)
                continue
            raise

    if last_exc and not raw_text:
        raise last_exc

    log.info(
        "LLM fixer raw response",
        extra={"test": failure.get("test_name"), "response_chars": len(raw_text)},
    )

    proposal, parse_ok = _parse_fixer_json(raw_text, failure.get("test_name", ""))
    proposal["parse_ok"] = parse_ok
    proposal["actionable"] = _proposal_is_actionable(proposal)
    return proposal


def _apply_fix_proposal(java_source: str, proposal: dict[str, Any], test_name: str) -> dict[str, Any]:
    """Apply a parsed LLM proposal to source; same return shape as fix_test_file."""
    if not proposal.get("parse_ok", True):
        return {
            "patched_source": java_source,
            "changes_summary": proposal.get("changes_summary", "LLM response parse error."),
            "replacements_applied": 0,
            "schema_updates": [],
            "parse_ok": False,
            "actionable": False,
        }

    replacements: list[dict] = proposal.get("replacements", [])
    schema_updates: list[dict] = proposal.get("schema_updates", [])
    changes_summary: str = proposal.get("changes_summary", "")

    replacements_applied = 0
    patched_source = java_source
    if replacements:
        try:
            patched_source, descriptions = apply_replacements(java_source, replacements)
            replacements_applied = len(descriptions)
            log.info(
                "Java replacements applied",
                extra={"test": test_name, "count": replacements_applied},
            )
        except ValueError as exc:
            return {
                "patched_source": java_source,
                "changes_summary": f"Could not apply replacements: {exc}",
                "replacements_applied": 0,
                "schema_updates": schema_updates,
                "parse_ok": True,
                "actionable": bool(schema_updates),
            }
    else:
        log.info(
            "No Java replacements in proposal",
            extra={"test": test_name, "schema_updates": len(schema_updates)},
        )

    actionable = replacements_applied > 0 or bool(schema_updates)
    if not actionable and not changes_summary:
        changes_summary = "Fixer produced no applicable Java or schema changes."

    return {
        "patched_source": patched_source,
        "changes_summary": changes_summary,
        "replacements_applied": replacements_applied,
        "schema_updates": schema_updates,
        "parse_ok": True,
        "actionable": actionable,
    }


def fix_test_file(failure: dict[str, Any], java_source: str) -> dict[str, Any]:
    """Single failure: LLM proposal then apply to source."""
    proposal = _invoke_fixer_llm(failure, java_source)
    return _apply_fix_proposal(
        java_source,
        proposal,
        failure.get("test_name", ""),
    )


def fix_failures_for_java_file(
    failures: list[dict[str, Any]],
    initial_source: str,
    llm_workers: Optional[int] = None,
) -> tuple[str, list[tuple[dict[str, Any], Optional[dict[str, Any]], Optional[Exception]]]]:
    """
    Fix all failures that share one Java file.

    - Multiple failures: LLM calls run in parallel (same initial source snapshot).
    - Replacements are applied sequentially in original failure order so patches chain safely.
    - Single failure: one LLM call, no thread pool.

    Returns (final_source, [(failure, fix_result_or_none, error_or_none), ...]).
    """
    if not failures:
        return initial_source, []

    workers = llm_workers if llm_workers is not None else settings.fixer_llm_parallel_per_file
    workers = max(1, min(workers, len(failures)))

    if workers == 1 or len(failures) == 1:
        accumulated = initial_source
        outcomes: list[tuple[dict[str, Any], Optional[dict[str, Any]], Optional[Exception]]] = []
        for failure in failures:
            try:
                fix_result = fix_test_file(failure, accumulated)
                outcomes.append((failure, fix_result, None))
                if fix_result["replacements_applied"]:
                    accumulated = fix_result["patched_source"]
            except Exception as exc:
                outcomes.append((failure, None, exc))
        return accumulated, outcomes

    # Parallel LLM proposals against the same pre-patch source
    proposals: dict[int, tuple[dict[str, Any], Optional[dict[str, Any]], Optional[Exception]]] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_to_idx = {
            pool.submit(_invoke_fixer_llm, failure, initial_source): idx
            for idx, failure in enumerate(failures)
        }
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            failure = failures[idx]
            try:
                proposals[idx] = (failure, future.result(), None)
            except Exception as exc:
                proposals[idx] = (failure, None, exc)

    accumulated = initial_source
    outcomes = []
    for idx in range(len(failures)):
        failure, proposal, err = proposals[idx]
        if err is not None:
            outcomes.append((failure, None, err))
            continue
        if proposal is None:
            outcomes.append((failure, None, RuntimeError("Missing fix proposal")))
            continue
        try:
            fix_result = _apply_fix_proposal(
                accumulated,
                proposal,
                failure.get("test_name", ""),
            )
            outcomes.append((failure, fix_result, None))
            if fix_result["replacements_applied"]:
                accumulated = fix_result["patched_source"]
        except Exception as exc:
            outcomes.append((failure, None, exc))

    return accumulated, outcomes


def process_java_file_patches(
    file_patches: dict[str, dict[str, Any]],
    file_workers: Optional[int] = None,
    llm_workers_per_file: Optional[int] = None,
) -> list[tuple[str, str, bool, list[tuple[dict[str, Any], Optional[dict[str, Any]], Optional[Exception]]]]]:
    """
    Process all Java files. Different files run in parallel; failures within a file
    use fix_failures_for_java_file (parallel LLM + sequential apply).

    Returns list of:
      (resolved_path, final_source, file_had_fix, outcomes)
    """
    fw = file_workers if file_workers is not None else settings.fixer_max_workers
    fw = max(1, fw)
    results: list[tuple[str, str, bool, list]] = []

    def _process_one(item: tuple[str, dict]) -> tuple[str, str, bool, list]:
        resolved_path, patch_data = item
        accumulated, outcomes = fix_failures_for_java_file(
            patch_data["failures"],
            patch_data["source"],
            llm_workers=llm_workers_per_file,
        )
        file_had_fix = any(
            r and r.get("replacements_applied", 0) > 0
            for _, r, err in outcomes
            if err is None and r
        )
        return resolved_path, accumulated, file_had_fix, outcomes

    items = list(file_patches.items())
    if fw == 1 or len(items) == 1:
        for item in items:
            results.append(_process_one(item))
        return results

    with ThreadPoolExecutor(max_workers=min(fw, len(items))) as pool:
        futures = [pool.submit(_process_one, item) for item in items]
        for future in as_completed(futures):
            results.append(future.result())

    return results

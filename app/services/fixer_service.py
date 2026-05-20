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
You are a QA engineer fixing API test assertion values.

STRICT RULES:
- ONLY change the assertion VALUE (the expected string, status code, or response body matcher)
- Do NOT change method names, variable names, imports, test structure, or comments
- Do NOT reformat, reindent, or change whitespace outside of the changed value
- Each old_snippet MUST be copied EXACTLY character-for-character from the Java source
- Each new_snippet must be identical to old_snippet except for the assertion value itself

Return a JSON object ONLY — no markdown, no explanation:
{{
  "replacements": [
    {{
      "old_snippet": "<exact substring from source, including surrounding context>",
      "new_snippet": "<same substring with only the assertion value updated>",
      "description": "<one line: what changed and why>"
    }}
  ],
  "changes_summary": "<concise bullet list of all changes for the PR body>"
}}

If no assertion change is needed, return: {{"replacements": [], "changes_summary": "No changes required."}}"""

_HUMAN_PROMPT = """Fix the failing assertion in this Java test file.

Test Name: {test_name}
Assertion Error (what failed): {assertion_error}
Actual API Response (the new correct value): {actual_response}

Java Source:
```java
{java_source}
```

Return only the JSON object with the targeted replacements."""


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
# Apply replacements
# ---------------------------------------------------------------------------

def apply_replacements(original_source: str, replacements: list[dict]) -> tuple[str, list[str]]:
    """
    Apply targeted string replacements to the original Java source.

    Returns (patched_source, list_of_descriptions).
    Raises ValueError if any old_snippet is not found in the source.
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
            raise ValueError(
                f"old_snippet not found in source file.\n"
                f"Snippet: {old[:120]!r}"
            )

        patched = patched.replace(old, new, 1)
        descriptions.append(desc)
        log.info("Applied replacement", extra={"description": desc})

    return patched, descriptions


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fix_test_file(
    failure: dict[str, Any],
    java_source: str,
) -> dict[str, Any]:
    """
    Call the LLM to get targeted replacements for one failing test,
    apply them to the original source, and return:
      {
        "patched_source": str,       # original file with only assertion values changed
        "changes_summary": str,      # bullet list for the PR body
        "replacements_applied": int, # number of replacements made
      }
    """
    chain = build_fixer_chain()
    raw: AIMessage = chain.invoke({
        "test_name": failure.get("test_name", ""),
        "assertion_error": failure.get("assertion_error", "") or "",
        "actual_response": failure.get("actual_response", "") or "",
        "java_source": java_source,
    })

    raw_text: str = raw.content if isinstance(raw, AIMessage) else str(raw)
    log.info(
        "LLM fixer raw response",
        extra={"test": failure.get("test_name"), "response": raw_text},
    )

    # Parse JSON response
    try:
        result = json.loads(raw_text.strip())
    except json.JSONDecodeError:
        cleaned = (
            raw_text.strip()
            .removeprefix("```json")
            .removeprefix("```")
            .removesuffix("```")
            .strip()
        )
        result = json.loads(cleaned)

    replacements = result.get("replacements", [])
    changes_summary = result.get("changes_summary", "")

    if not replacements:
        log.info("No replacements returned by LLM", extra={"test": failure.get("test_name")})
        return {
            "patched_source": java_source,
            "changes_summary": changes_summary or "No assertion changes required.",
            "replacements_applied": 0,
        }

    patched_source, descriptions = apply_replacements(java_source, replacements)

    return {
        "patched_source": patched_source,
        "changes_summary": changes_summary,
        "replacements_applied": len(descriptions),
    }

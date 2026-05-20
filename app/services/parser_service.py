"""
ExtentReport v5 (Spark reporter) HTML parser.

Confirmed DOM structure from the actual sample report:
  - Test nodes:      <li class="test-item" status="fail">
  - Test name:       <p class="name"> inside the test node
  - Assertion error: <textarea class="code-block"> (the Java stack trace)
  - Class path:      extracted from the first stack-trace line that matches the
                     test name, e.g.
                     "io.recruitcrm.LoginTest.myTest(LoginTest.java:42)"
                     → "io/recruitcrm/LoginTest.java"
  - CURL command:    <details class="extent-http-capture"> whose <summary>
                     contains "curl" → inner <pre>
  - HTTP response:   <details class="extent-http-capture"> whose <summary>
                     contains "response" → inner <pre>

Run the offline validation script to confirm against new reports:
    python scripts/test_parser.py path/to/report.html
"""
import re
from typing import Optional

from bs4 import BeautifulSoup, Tag

from app.logger import get_logger
from app.models.test_failure import TestFailure

log = get_logger(__name__)

# Matches a Java FQCN stack-trace line, e.g.
#   io.recruitcrm.account.LoginTest.myTestMethod(LoginTest.java:100)
_STACK_LINE_RE = re.compile(
    r'([\w]+(?:\.[\w]+)+)\.([\w$<>]+)\([\w]+\.java:\d+\)'
)


def _extract_class_path(stack_trace: str, test_name: str) -> str:
    """
    Walk stack-trace lines to find the first line whose method name matches
    (or contains) the test name. Return the FQCN as a slash-separated path.

    Falls back to the first line that looks like a test class if no exact match.
    """
    test_name_lower = test_name.lower()
    first_candidate = ""

    for line in stack_trace.splitlines():
        m = _STACK_LINE_RE.search(line.strip())
        if not m:
            continue
        fqcn = m.group(1)          # e.g. "io.recruitcrm.account.LoginTest"
        method = m.group(2)        # e.g. "myTestMethod"

        if not first_candidate:
            first_candidate = fqcn

        # Prefer the line whose method is the actual test method
        if method.lower() == test_name_lower or test_name_lower in method.lower():
            return fqcn.replace(".", "/") + ".java"

    if first_candidate:
        return first_candidate.replace(".", "/") + ".java"

    return ""


def _extract_http_blocks(test_node: Tag) -> tuple[Optional[str], Optional[str]]:
    """
    Return (curl_command, actual_response) from the extent-http-capture blocks.
    Each block is a <details> whose <summary> labels it as curl or response.
    """
    curl_command: Optional[str] = None
    actual_response: Optional[str] = None

    for details in test_node.find_all("details", class_="extent-http-capture"):
        summary = details.find("summary")
        if not summary:
            continue
        label = summary.get_text(strip=True).lower()
        pre = details.find("pre")
        if not pre:
            continue
        text = pre.get_text(strip=True)
        if not text:
            continue

        if "curl" in label and curl_command is None:
            curl_command = text
        elif "response" in label and actual_response is None:
            actual_response = text

    return curl_command, actual_response


def _extract_assertion_error(test_node: Tag) -> Optional[str]:
    """
    Return the text from the first <textarea class="code-block"> — this is the
    Java exception / stack trace that is the root cause of the failure.
    """
    textarea = test_node.find("textarea", class_="code-block")
    if textarea:
        return textarea.get_text(strip=True) or None
    return None


def parse_extent_report(file_path: str, job_id: str) -> list[TestFailure]:
    """
    Parse an ExtentReport v5 Spark HTML file.
    Returns one TestFailure per unique failed <li class="test-item"> node.
    """
    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        soup = BeautifulSoup(f, "lxml")

    # The only nodes that represent real test cases
    test_nodes = soup.find_all("li", class_="test-item", attrs={"status": "fail"})
    log.info("Found failed test-item nodes", extra={"count": len(test_nodes), "report_file": file_path})

    failures: list[TestFailure] = []

    for node in test_nodes:
        # --- Test name ---
        name_tag = node.find("p", class_="name")
        test_name = name_tag.get_text(strip=True) if name_tag else ""
        if not test_name:
            log.warning("Could not extract test name from node, skipping", extra={"node_id": node.get("test-id")})
            continue

        # --- Assertion error / stack trace ---
        assertion_error = _extract_assertion_error(node)

        # --- Class path (derived from stack trace) ---
        test_class_path = ""
        if assertion_error:
            test_class_path = _extract_class_path(assertion_error, test_name)

        if not test_class_path:
            log.warning("Could not resolve class path", extra={"test": test_name})

        # --- CURL + HTTP response ---
        curl_command, actual_response = _extract_http_blocks(node)

        failures.append(TestFailure(
            job_id=job_id,
            test_name=test_name,
            test_class_path=test_class_path,
            curl_command=curl_command,
            actual_response=actual_response,
            assertion_error=assertion_error,
        ))

    log.info(
        "Parsing complete",
        extra={
            "report_file": file_path,
            "failures_extracted": len(failures),
        },
    )
    return failures

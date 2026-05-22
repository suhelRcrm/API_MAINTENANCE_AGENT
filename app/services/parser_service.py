"""
ExtentReport v5 (Spark reporter) HTML parser.

Confirmed DOM structure from the actual sample report:
  - Test nodes:      <li class="test-item" status="fail">
  - Test name:       <p class="name"> inside the test node
  - Assertion error: <textarea class="code-block"> plus script-failed row and curl
                     stackTrace/failureType when the textarea is minimal
  - Class path:      resolved in order — (1) Java stack trace in code-block,
                     (2) "testClass" in the curl JSON payload, (3) "Script failed
                     at method: ClassName.method" in a fail event row
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
# "Script failed at method: GetPitchCandidateContactsTest.testFoo"
_SCRIPT_FAILED_RE = re.compile(
    r"Script failed at method:\s*([\w$]+)\.([\w$]+)",
    re.IGNORECASE,
)
# testClass field inside the curl -d JSON body
_TEST_CLASS_JSON_RE = re.compile(r'"testClass"\s*:\s*"([^"]+)"')
_DATA_ROW_SUFFIX_RE = re.compile(r"\s+\[data row \d+\]$", re.IGNORECASE)
_FAILURE_TYPE_JSON_RE = re.compile(r'"failureType"\s*:\s*"([^"]+)"')
_STACK_TRACE_JSON_RE = re.compile(r'"stackTrace"\s*:\s*"((?:\\.|[^"\\])*)"')


def _base_test_name(test_name: str) -> str:
    """Strip data-provider suffix so method matching works."""
    return _DATA_ROW_SUFFIX_RE.sub("", test_name).strip()


def _fqcn_to_class_path(fqcn: str) -> str:
    """Convert io.recruitcrm.FooTest to io/recruitcrm/FooTest.java."""
    fqcn = fqcn.strip()
    if not fqcn:
        return ""
    if fqcn.endswith(".java"):
        return fqcn.replace(".", "/") if "/" not in fqcn else fqcn
    return fqcn.replace(".", "/") + ".java"


def _extract_class_path_from_stack(stack_trace: str, test_name: str) -> str:
    """
    Walk stack-trace lines to find the first line whose method name matches
    (or contains) the test name. Return the FQCN as a slash-separated path.

    Falls back to the first line that looks like a test class if no exact match.
    """
    test_name_lower = _base_test_name(test_name).lower()
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


def _extract_class_path_from_curl(curl_command: str) -> str:
    """Read testClass from the JSON body embedded in the curl command."""
    m = _TEST_CLASS_JSON_RE.search(curl_command)
    if m:
        return _fqcn_to_class_path(m.group(1))
    return ""


def _extract_class_path_from_script_failed(test_node: Tag, _test_name: str) -> str:
    """
    Parse "Script failed at method: SimpleClass.method" from fail event rows.
    Returns a filename-only path (e.g. GetPitchCandidateContactsTest.java).
    """

    for row in test_node.find_all("tr", class_="event-row"):
        for cell in row.find_all("td"):
            m = _SCRIPT_FAILED_RE.search(cell.get_text(strip=True))
            if not m:
                continue
            simple_class = m.group(1)
            return f"{simple_class}.java"

    return ""


def _resolve_test_class_path(
    test_node: Tag,
    assertion_error: Optional[str],
    test_name: str,
    curl_command: Optional[str],
) -> str:
    """
    Resolve test_class_path using stack trace, then curl testClass, then
    script-failed line (first match wins).
    """
    if assertion_error:
        path = _extract_class_path_from_stack(assertion_error, test_name)
        if path:
            return path

    if curl_command:
        path = _extract_class_path_from_curl(curl_command)
        if path:
            return path

    return _extract_class_path_from_script_failed(test_node, test_name)


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


def _extract_script_failed_line(test_node: Tag) -> Optional[str]:
    for row in test_node.find_all("tr", class_="event-row"):
        for cell in row.find_all("td"):
            text = cell.get_text(strip=True)
            if "Script failed at method:" in text:
                return text
    return None


def _decode_json_string(value: str) -> str:
    return value.replace("\\n", "\n").replace("\\t", "\t").replace('\\"', '"')


def _extract_curl_diagnostic_fields(curl_command: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    if not curl_command:
        return None, None
    failure_type = None
    stack_trace = None
    m = _FAILURE_TYPE_JSON_RE.search(curl_command)
    if m:
        failure_type = m.group(1)
    m = _STACK_TRACE_JSON_RE.search(curl_command)
    if m:
        stack_trace = _decode_json_string(m.group(1)).strip()
    return failure_type, stack_trace


def _build_assertion_error(
    test_node: Tag,
    curl_command: Optional[str],
) -> Optional[str]:
    """
    Combine textarea exception, script-failed line, and curl JSON diagnostics
    so the classifier sees enough context for NPE/CCE/IOOBE failures.
    """
    parts: list[str] = []
    textarea = test_node.find("textarea", class_="code-block")
    base = textarea.get_text(strip=True) if textarea else ""
    if base:
        parts.append(base)

    script_line = _extract_script_failed_line(test_node)
    if script_line and script_line not in parts:
        parts.append(script_line)

    failure_type, stack_trace = _extract_curl_diagnostic_fields(curl_command)
    if failure_type and failure_type not in base and failure_type not in "\n".join(parts):
        parts.append(failure_type)
    if stack_trace and stack_trace not in base and stack_trace not in "\n".join(parts):
        parts.append(stack_trace)

    merged = "\n".join(parts).strip()
    return merged or None


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

        # --- CURL + HTTP response (curl may carry testClass for class-path fallback) ---
        curl_command, actual_response = _extract_http_blocks(node)

        # --- Assertion error (textarea + script-failed + curl diagnostics) ---
        assertion_error = _build_assertion_error(node, curl_command)

        # --- Class path: stack trace → curl testClass → script-failed line ---
        test_class_path = _resolve_test_class_path(
            node, assertion_error, test_name, curl_command
        )

        if not test_class_path:
            log.warning("Could not resolve class path", extra={"test": test_name})

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

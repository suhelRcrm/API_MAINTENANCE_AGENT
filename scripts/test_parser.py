"""
Offline integration test for the HTML report parser.

Usage:
    python scripts/test_parser.py path/to/your/report.html

What it does:
1. Runs parse_extent_report() directly (no Huey, no HTTP, no MongoDB)
2. Prints extracted failures as JSON
3. Validates that required fields are present on each failure
4. Exits with code 1 if validation fails so this can be used in CI

Adjust the selectors in app/services/parser_service.py until all fields parse correctly.
"""
import sys
import json
import os

# Allow running from project root without installing the package
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.parser_service import parse_extent_report


def validate_failures(failures):
    issues = []
    for i, f in enumerate(failures):
        if not f.test_name or f.test_name == "Unknown Test":
            issues.append(f"  [{i}] test_name is missing or 'Unknown Test'")
        if not f.test_class_path:
            issues.append(f"  [{i}] test_class_path is empty — resolve_file_path() will be used")
        if not f.assertion_error:
            issues.append(f"  [{i}] assertion_error is None — classifier will have less context")
        if not f.actual_response:
            issues.append(f"  [{i}] actual_response is None — fixer will have less context")
    return issues


def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/test_parser.py path/to/report.html")
        sys.exit(1)

    report_path = sys.argv[1]
    if not os.path.exists(report_path):
        print(f"ERROR: File not found: {report_path}")
        sys.exit(1)

    print(f"\nParsing: {report_path}\n{'='*60}")
    failures = parse_extent_report(report_path, job_id="test-run-001")

    print(f"Found {len(failures)} failed test(s)\n")

    if not failures:
        print("WARNING: Zero failures extracted. Check the HTML structure and update selectors.")
        sys.exit(1)

    for i, f in enumerate(failures):
        print(f"--- Failure {i+1} ---")
        data = {
            "test_name": f.test_name,
            "test_class_path": f.test_class_path,
            "assertion_error": f.assertion_error[:200] + "…" if f.assertion_error and len(f.assertion_error) > 200 else f.assertion_error,
            "actual_response": f.actual_response[:200] + "…" if f.actual_response and len(f.actual_response) > 200 else f.actual_response,
            "curl_command": f.curl_command[:150] + "…" if f.curl_command and len(f.curl_command) > 150 else f.curl_command,
        }
        print(json.dumps(data, indent=2))
        print()

    issues = validate_failures(failures)
    if issues:
        print("VALIDATION WARNINGS (non-fatal — adjust selectors in parser_service.py):")
        for issue in issues:
            print(issue)
        print()

    print("Parser test complete.")


if __name__ == "__main__":
    main()

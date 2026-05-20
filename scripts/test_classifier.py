"""
Offline integration test for the LLM classification chain.

Usage:
    python scripts/test_classifier.py

Requires:
    - GOOGLE_API_KEY set in .env
    - Dependencies installed: pip install -r requirements.txt

What it does:
1. Runs two known test cases through the classification chain:
   - A SAFE_TO_FIX case (negative scenario, only error message changed)
   - A BACKEND_BUG case (happy path regression)
2. Prints the guardrail boolean breakdown for each
3. Asserts the expected classification is returned
4. Exits with code 1 if any assertion fails
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Load .env
from dotenv import load_dotenv
load_dotenv()

from app.services.classifier_service import build_classification_chain, get_parser
from app.models.test_failure import Classification


SAFE_TO_FIX_CASE = {
    "test_name": "testCreateOrder_withInvalidSku_returns400",
    "assertion_error": (
        'Expected: "SKU not found"\n'
        'Actual:   "Product SKU does not exist in catalog"'
    ),
    "actual_response": '{"error": "Product SKU does not exist in catalog", "code": 400}',
    "curl_command": (
        'curl -X POST https://api.example.com/orders '
        '-H "Content-Type: application/json" '
        '-d \'{"sku": "INVALID-999", "qty": 1}\''
    ),
    "java_source": """\
@Test
public void testCreateOrder_withInvalidSku_returns400() {
    Response response = given()
        .body(new OrderRequest("INVALID-999", 1))
        .post("/orders");
    
    response.then()
        .statusCode(400)
        .body("error", equalTo("SKU not found"));
}
""",
    "expected_classification": Classification.SAFE_TO_FIX,
}

BACKEND_BUG_CASE = {
    "test_name": "testCreateOrder_withValidData_returns201",
    "assertion_error": (
        'Expected status code: 201\n'
        'Actual status code: 500'
    ),
    "actual_response": '{"error": "Internal server error", "code": 500}',
    "curl_command": (
        'curl -X POST https://api.example.com/orders '
        '-H "Content-Type: application/json" '
        '-d \'{"sku": "WIDGET-001", "qty": 1}\''
    ),
    "java_source": """\
@Test
public void testCreateOrder_withValidData_returns201() {
    Response response = given()
        .body(new OrderRequest("WIDGET-001", 1))
        .post("/orders");
    
    response.then()
        .statusCode(201)
        .body("orderId", notNullValue());
}
""",
    "expected_classification": Classification.BACKEND_BUG,
}


def run_case(chain, parser, case: dict) -> bool:
    print(f"\nTest: {case['test_name']}")
    print(f"Expected classification: {case['expected_classification']}")
    print("-" * 50)

    result = chain.invoke({
        "test_name": case["test_name"],
        "assertion_error": case["assertion_error"],
        "actual_response": case["actual_response"],
        "curl_command": case["curl_command"],
        "java_source": case["java_source"],
        "format_instructions": parser.get_format_instructions(),
    })

    print(f"  LLM classification:       {result.classification}")
    print(f"  is_happy_path:            {result.is_happy_path}")
    print(f"  is_security_rule:         {result.is_security_rule}")
    print(f"  is_response_type_changed: {result.is_response_type_changed}")
    print(f"  Reasoning:\n    {result.reasoning[:300]}…\n" if len(result.reasoning) > 300 else f"  Reasoning: {result.reasoning}\n")

    # Apply same guardrail logic as the Huey task
    if result.is_happy_path or result.is_security_rule or result.is_response_type_changed:
        actual_classification = Classification.BACKEND_BUG
    else:
        actual_classification = Classification(result.classification)

    passed = actual_classification == case["expected_classification"]
    status = "PASS" if passed else "FAIL"
    print(f"  Guardrail result: {actual_classification}  [{status}]")
    return passed


def main():
    print("Building classification chain…")
    chain = build_classification_chain()
    parser = get_parser()

    results = []
    for case in [SAFE_TO_FIX_CASE, BACKEND_BUG_CASE]:
        try:
            passed = run_case(chain, parser, case)
            results.append(passed)
        except Exception as e:
            print(f"  ERROR: {e}")
            results.append(False)

    print("\n" + "=" * 50)
    passed_count = sum(results)
    print(f"Results: {passed_count}/{len(results)} passed")

    if not all(results):
        print("FAILED — review the classifier prompt or guardrail logic.")
        sys.exit(1)
    else:
        print("All classification tests passed.")


if __name__ == "__main__":
    main()

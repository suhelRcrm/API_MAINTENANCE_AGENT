"""
Offline integration test for GitHub operations.

Usage:
    python scripts/test_github.py <java-class-name> [--full-roundtrip]

Arguments:
    java-class-name     Simple class name to resolve, e.g. "OrderApiTest"
    --full-roundtrip    Also creates a test branch, pushes a dummy commit,
                        creates a draft PR, then deletes the branch.
                        WARNING: This creates real objects in your GitHub repo.

Requires:
    - GITHUB_PAT, GITHUB_REPO_OWNER, GITHUB_REPO_NAME set in .env
    - Dependencies installed: pip install -r requirements.txt

What it does:
    1. Calls resolve_file_path() to find the .java file path
    2. Calls fetch_java_file() to verify content is returned
    3. (Optional) Full round-trip: create branch → push dummy commit → draft PR → delete branch
"""
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from app.services.github_service import (
    resolve_file_path,
    fetch_java_file,
    clone_repository,
    create_remote_branch,
    write_fixed_file,
    commit_and_push_fixes,
    create_pull_request,
)
from app.config import settings
from github import Github


def test_file_fetch(class_name: str) -> str:
    print(f"\n[1] Resolving path for class: {class_name}")
    resolved = resolve_file_path(class_name + ".java")
    print(f"    Resolved path: {resolved}")

    print(f"\n[2] Fetching file content from GitHub…")
    content = fetch_java_file(resolved)
    print(f"    Content length: {len(content)} chars")
    print(f"    First 200 chars:\n    {content[:200]!r}")
    return resolved


def test_full_roundtrip(resolved_path: str):
    timestamp = int(time.time())
    branch_name = f"test-github-integration-{timestamp}"
    local_path = os.path.join(settings.repos_dir, f"roundtrip-{timestamp}")

    print(f"\n[3] Full round-trip test")
    print(f"    Branch: {branch_name}")

    try:
        print("    Cloning repository…")
        local_repo = clone_repository(local_path)
        print(f"    Cloned to: {local_path}")

        print("    Creating remote branch via PyGithub…")
        create_remote_branch(branch_name)
        print("    Remote branch created.")

        print("    Writing dummy change…")
        dummy_content = fetch_java_file(resolved_path) + "\n// Integration test dummy change\n"
        write_fixed_file(local_path, resolved_path, dummy_content)

        print("    Committing and pushing…")
        commit_and_push_fixes(local_repo, branch_name, [resolved_path], job_id="roundtrip-test")
        print("    Push complete.")

        print("    Creating draft PR…")
        g = Github(settings.github_pat)
        repo = g.get_repo(f"{settings.github_repo_owner}/{settings.github_repo_name}")
        pr = repo.create_pull(
            title=f"[Integration Test] {branch_name}",
            body="Automated round-trip integration test. Safe to close/delete.",
            head=branch_name,
            base=settings.github_default_branch,
            draft=True,
        )
        print(f"    Draft PR created: {pr.html_url}")

        print("    Closing PR and deleting branch…")
        pr.edit(state="closed")
        repo.get_git_ref(f"heads/{branch_name}").delete()
        print("    Branch deleted. Round-trip complete.")

    finally:
        import shutil
        if os.path.exists(local_path):
            shutil.rmtree(local_path)
            print(f"    Local clone cleaned up.")


def main():
    args = sys.argv[1:]
    if not args:
        print("Usage: python scripts/test_github.py <ClassName> [--full-roundtrip]")
        sys.exit(1)

    class_name = args[0]
    do_roundtrip = "--full-roundtrip" in args

    try:
        resolved_path = test_file_fetch(class_name)
        if do_roundtrip:
            test_full_roundtrip(resolved_path)
        else:
            print("\nSkipping round-trip (pass --full-roundtrip to enable).")
        print("\nGitHub integration tests passed.")
    except Exception as e:
        print(f"\nERROR: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()

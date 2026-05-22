from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    mongodb_url: str
    db_name: str
    secret_key: str
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 480
    google_api_key: str = ""
    llm_model_name: str = "gemini-1.5-pro"
    # Classification: chunk size + delay to stay under Gemini free-tier TPM
    classify_batch_size: int = 75
    classify_batch_delay_seconds: float = 25.0
    classify_response_max_chars: int = 200
    classify_llm_max_retries: int = 5
    # Fixer: parallel LLM calls (per file + across files); patches still applied in order
    fixer_max_workers: int = 4
    fixer_llm_parallel_per_file: int = 3
    fixer_llm_max_retries: int = 5
    github_pat: str
    github_repo_owner: str
    github_repo_name: str
    github_default_branch: str = "main"
    git_clone_max_retries: int = 4
    git_clone_retry_delay_seconds: float = 15.0
    github_clone_depth: int = 1
    reports_dir: str = "./reports"
    repos_dir: str = "./repos"

    class Config:
        env_file = ".env"


settings = Settings()

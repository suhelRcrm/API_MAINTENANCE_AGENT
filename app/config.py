from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    mongodb_url: str
    db_name: str
    secret_key: str
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 480
    google_api_key: str = ""
    llm_model_name: str = "gemini-1.5-pro"
    github_pat: str
    github_repo_owner: str
    github_repo_name: str
    github_default_branch: str = "main"
    reports_dir: str = "./reports"
    repos_dir: str = "./repos"

    class Config:
        env_file = ".env"


settings = Settings()

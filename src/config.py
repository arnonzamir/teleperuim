from __future__ import annotations

from enum import Enum

from pydantic import Field
from pydantic_settings import BaseSettings


class BackendType(str, Enum):
    UNOFFICIAL = "unofficial"
    OFFICIAL = "official"


class Settings(BaseSettings):
    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    # Required
    instagram_backend: BackendType = BackendType.UNOFFICIAL
    api_key: str = "changeme"

    # Unofficial backend credentials
    instagram_username: str = ""
    instagram_password: str = ""

    # Official backend credentials
    meta_access_token: str = ""
    meta_app_secret: str = ""
    instagram_business_account_id: str = ""

    # Webhook
    webhook_url: str = ""
    webhook_secret: str = ""

    # Operational
    log_level: str = "INFO"
    poll_interval_seconds: int = 10
    rate_limit_per_hour: int = 20
    data_dir: str = "/data"
    proxy_url: str = ""
    instance_id: str = "main"

    # Derived paths
    @property
    def sessions_dir(self) -> str:
        return f"{self.data_dir}/sessions"

    @property
    def queue_db_path(self) -> str:
        return f"{self.data_dir}/queue.db"

    @property
    def webhook_failures_dir(self) -> str:
        return f"{self.data_dir}/webhook_failures"

    @property
    def media_dir(self) -> str:
        return f"{self.data_dir}/media"


settings = Settings()

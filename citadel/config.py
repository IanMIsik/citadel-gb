"""Environment-based settings. Reads a local .env if present (see
.env.template) but never requires one -- every field has a default that
runs the app in REST-only mode against a local Postgres.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql://citadel:citadel@localhost:5432/citadel"

    iris_tenant_id: str = "4203b7a0-7773-4de5-b830-8b263a20426e"
    iris_namespace: str = "elexon-insights-iris"
    iris_client_id: str = ""
    iris_client_secret: str = ""
    iris_queue_name: str = ""

    rest_poll_interval_seconds: int = 5

    @property
    def iris_configured(self) -> bool:
        return bool(self.iris_client_id and self.iris_client_secret and self.iris_queue_name)


settings = Settings()

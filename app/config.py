import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_path: str = os.getenv("DATABASE_PATH", "./data/monitor.db")
    check_interval: int = int(os.getenv("CHECK_INTERVAL", "15"))
    app_secret: str = os.getenv("APP_SECRET", "")
    webhook_encryption_key: str = os.getenv("WEBHOOK_ENCRYPTION_KEY", "")
    collector_secret: str = os.getenv("COLLECTOR_SECRET", "")
    collector_stale_after: int = int(os.getenv("COLLECTOR_STALE_AFTER", "90"))
    cookie_secure: bool = os.getenv("COOKIE_SECURE", "false").lower() == "true"
    timezone: str = "Asia/Jakarta"


settings = Settings()

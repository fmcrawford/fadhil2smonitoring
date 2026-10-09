import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_path: str = os.getenv("DATABASE_PATH", "./data/monitor.db")
    check_interval: int = int(os.getenv("CHECK_INTERVAL", "15"))
    app_secret: str = os.getenv("APP_SECRET", "")
    webhook_encryption_key: str = os.getenv("WEBHOOK_ENCRYPTION_KEY", "")
    collector_secret: str = os.getenv("COLLECTOR_SECRET", "")
    collector_stale_after: int = int(os.getenv("COLLECTOR_STALE_AFTER", "300"))
    hybrid_source_offline_after: int = int(
        os.getenv("HYBRID_SOURCE_OFFLINE_AFTER", "150")
    )
    hybrid_failover_failures: int = int(
        os.getenv("HYBRID_FAILOVER_FAILURES", "2")
    )
    hybrid_cloud_recovery_successes: int = int(
        os.getenv("HYBRID_CLOUD_RECOVERY_SUCCESSES", "3")
    )
    cookie_secure: bool = os.getenv("COOKIE_SECURE", "false").lower() == "true"
    timezone: str = "Asia/Jakarta"


settings = Settings()

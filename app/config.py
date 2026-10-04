"""应用配置。

通过环境变量覆盖默认值，Compose 中默认指向 PostgreSQL 服务。
"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", case_sensitive=False)

    # 形如 postgresql://user:pass@host:port/dbname
    database_url: str = "postgresql://water:water@localhost:5432/water"
    db_connect_retries: int = 30
    db_connect_retry_interval: float = 1.0
    # 历史记录接口最多返回条数
    history_limit: int = 100


@lru_cache
def get_settings() -> Settings:
    return Settings()

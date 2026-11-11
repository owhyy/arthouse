from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# src/arthouse/config.py -> src/arthouse -> src -> project root
BASE_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(  # pyright: ignore[reportUnannotatedClassAttribute]
        env_file=BASE_DIR / ".env",
        env_prefix="ARTHOUSE_",
        extra="ignore",
    )

    debug: bool = False

    data_dir: Path = BASE_DIR / "data"

    # TMDB: v3 API key or v4 read access token (Bearer), for film genres.
    tmdb_api_key: str = ""

    # Server
    host: str = "127.0.0.1"
    port: int = 8000


@lru_cache
def get_settings() -> Settings:
    return Settings()

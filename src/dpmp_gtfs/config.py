"""Runtime configuration, read from the environment."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DPMP_", env_file=".env", extra="ignore")

    # --- upstream API -------------------------------------------------------
    api_root: str = "https://online.dpmp.cz/api"
    api_key: str = ""
    """Access key for the DPMP API.

    The public web app ships this value hardcoded in its JS bundle, so it is not
    a secret in any meaningful sense -- but it stays out of the source tree
    anyway, so that rotating it does not require a code change.
    """

    user_agent: str = "dpmp-to-gtfsr/0.1 (+https://github.com/xaralis/dpmp-to-gtfsr)"

    # --- politeness ---------------------------------------------------------
    request_timeout: float = 15.0
    max_retries: int = 4
    retry_backoff: float = 2.0
    """Base for exponential backoff, in seconds: 2, 4, 8, 16..."""

    crawl_concurrency: int = 4
    """Kept deliberately low. Eight parallel connections made the upstream
    time out during exploration."""

    crawl_delay: float = 0.1
    """Pause between crawl requests, in seconds."""

    # --- refresh cadence ----------------------------------------------------
    realtime_interval: float = 15.0
    static_rebuild_hour: int = 3
    """Local hour at which the static feed is rebuilt."""

    public_url: str = ""
    """Public base URL, e.g. https://gtfs.example.cz.

    Only needed for absolute URLs in social preview tags; everything else on
    the site is relative and works without it. Left empty, those tags are
    omitted rather than emitted pointing at localhost."""

    # --- shapes -------------------------------------------------------------
    shapes_enabled: bool = True
    """Route trip geometry against OpenStreetMap.

    Results are cached by stop sequence, so a rebuild whose routes have not
    changed issues no routing requests at all."""

    # --- output -------------------------------------------------------------
    data_dir: Path = Path("data")

    @property
    def gtfs_zip_path(self) -> Path:
        return self.data_dir / "gtfs.zip"


settings = Settings()

"""Runtime configuration, read from the environment."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DPMP_", env_file=".env", extra="ignore")

    # --- upstream API -------------------------------------------------------
    api_root: str = "https://api.mhdonline.cz"
    provider: str = "pardubice"
    """Path prefix the new API groups everything under. They also publish
    ``kromeriz`` and an aggregate ``global``; we only ever want Pardubice."""

    protocol_seed: str = "your-public-protocol-seed"
    """HMAC seed for the ``X-App-Protocol`` header -- see
    :mod:`dpmp_gtfs.protocol`. Public, but kept in settings so it can be
    swapped without a code change."""

    user_agent: str = "dpmp-to-gtfsr/0.1 (+https://github.com/xaralis/dpmp-to-gtfsr)"

    # --- CIS JŘ registry ----------------------------------------------------
    cis_urls: tuple[str, ...] = (
        "https://portal.cisjr.cz/pub/netex/NeTEx_DrahyMestske.zip",
        "https://portal.cisjr.cz/pub/netex/NeTEx_VerejnaLinkovaDoprava.zip",
    )
    """Trolejbusy jsou v prvním archivu, autobusy ve druhém. Oba jsou potřeba."""

    cis_dir: Path = Path("data/cis")

    # --- politeness ---------------------------------------------------------
    request_timeout: float = 15.0
    max_retries: int = 4
    retry_backoff: float = 2.0
    """Base for exponential backoff, in seconds: 2, 4, 8, 16..."""

    crawl_concurrency: int = 8
    crawl_rate_limit: float = 8.0
    """Sustained requests per second. Expressed as a rate rather than as a
    sleep between requests: with N concurrent workers a fixed sleep makes the
    real rate depend on latency."""

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

    http_cache: bool = False
    """Replay API responses from disk -- see :mod:`dpmp_gtfs.api.cache`.

    A development aid: a full crawl is ~20 minutes, so investigating anything
    that surfaces late in a build is unaffordable without it. Off by default
    because a scheduled rebuild must fetch the timetable that exists now."""

    @property
    def gtfs_zip_path(self) -> Path:
        return self.data_dir / "gtfs.zip"

    @property
    def http_cache_dir(self) -> Path:
        return self.data_dir / "http-cache"


settings = Settings()

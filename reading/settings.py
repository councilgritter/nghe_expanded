"""Configuration for the reading module, read from the environment.

No secrets live in this file.  Everything comes from the process environment or
from a ``reading/.env`` file (loaded here by a ~30-line stdlib parser so the
module needs no python-dotenv dependency).

    from reading.settings import settings
    print(settings.deepseek_model)
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parent
REPO_ROOT = MODULE_DIR.parent


def load_dotenv(path: Path) -> int:
    """Load ``KEY=value`` lines into ``os.environ`` without overwriting real env vars.

    Deliberately minimal: comments, blank lines and an optional ``export`` prefix.
    Returns the number of variables set.
    """
    if not path.is_file():
        return 0
    set_count = 0
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
            set_count += 1
    return set_count


load_dotenv(MODULE_DIR / ".env")


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    """Resolved settings.  Instantiate once via :data:`settings`."""

    # --- storage -----------------------------------------------------------
    db_path: Path = field(
        default_factory=lambda: Path(
            os.environ.get("READING_DB_PATH")
            or (MODULE_DIR / "data" / "reading.sqlite3")
        )
    )
    # SQLite file holding the compound dictionary (built by tools/build_dictionary.py).
    dict_path: Path = field(
        default_factory=lambda: Path(
            os.environ.get("READING_DICT_PATH")
            or (MODULE_DIR / "data" / "dictionary.sqlite3")
        )
    )
    # Where tools/build_site.py writes the static artifacts the reading page loads.
    site_dir: Path = field(
        default_factory=lambda: Path(
            os.environ.get("READING_SITE_DIR") or (MODULE_DIR / "data" / "site")
        )
    )

    # --- DeepSeek ----------------------------------------------------------
    deepseek_api_key: str = field(
        default_factory=lambda: os.environ.get("DEEPSEEK_API_KEY", "")
    )
    deepseek_base_url: str = field(
        default_factory=lambda: os.environ.get(
            "DEEPSEEK_BASE_URL", "https://api.deepseek.com"
        ).rstrip("/")
    )
    # Model names follow the current pricing page.  `deepseek-chat` was the old
    # alias; `deepseek-flash` is what the docs name now, and `deepseek-v4-pro`
    # costs roughly 4x for work this pipeline does not need.
    # https://api-docs.deepseek.com/quick_start/pricing/
    deepseek_model: str = field(
        default_factory=lambda: os.environ.get("DEEPSEEK_MODEL", "deepseek-flash")
    )
    deepseek_timeout_s: float = field(
        default_factory=lambda: float(os.environ.get("DEEPSEEK_TIMEOUT_S", "120"))
    )
    # Retries for transient 429/5xx.  Kept low: ingest is batch, not interactive.
    deepseek_max_retries: int = field(
        default_factory=lambda: int(os.environ.get("DEEPSEEK_MAX_RETRIES", "3"))
    )

    # --- ingest sources ----------------------------------------------------
    # Verified live 2026-10.  VOA publishes obfuscated /api/<id> feeds; the ids
    # below are the "Tin tức" and "Việt Nam" channels from voatiengviet.com/rssfeeds.
    voa_rss_urls: tuple[str, ...] = field(
        default_factory=lambda: _split(
            os.environ.get(
                "VOA_RSS_URLS",
                "https://www.voatiengviet.com/api/zkvypl-vomx-tpejqup,"
                "https://www.voatiengviet.com/api/zbuyil-vomx-tpeqpum",
            )
        )
    )
    bbc_rss_urls: tuple[str, ...] = field(
        default_factory=lambda: _split(
            os.environ.get(
                "BBC_RSS_URLS", "https://feeds.bbci.co.uk/vietnamese/rss.xml"
            )
        )
    )
    fetch_full_text: bool = field(default_factory=lambda: _bool("READING_FETCH_FULL", True))
    http_timeout_s: float = field(
        default_factory=lambda: float(os.environ.get("READING_HTTP_TIMEOUT_S", "30"))
    )
    user_agent: str = field(
        default_factory=lambda: os.environ.get(
            "READING_USER_AGENT",
            "nghe-reading/0.1 (+https://github.com/councilgritter/nghe_expanded)",
        )
    )
    # Politeness delay between article fetches, seconds.
    fetch_delay_s: float = field(
        default_factory=lambda: float(os.environ.get("READING_FETCH_DELAY_S", "1.0"))
    )

    # --- segmentation ------------------------------------------------------
    # Longest headword the dictionary will enforce as a compound boundary.
    # The bundled word lists contain whole proverbs (up to 17 syllables); letting
    # those win would swallow entire clauses, so they are excluded by default.
    max_compound_syllables: int = field(
        default_factory=lambda: int(os.environ.get("READING_MAX_COMPOUND_SYLLABLES", "4"))
    )
    default_cefr: str = field(
        default_factory=lambda: os.environ.get("READING_DEFAULT_CEFR", "B1")
    )
    # Max articles ingested per run (0 = unlimited).  Keeps a first run cheap.
    ingest_limit: int = field(
        default_factory=lambda: int(os.environ.get("READING_INGEST_LIMIT", "0"))
    )

    # --- off-peak guard ----------------------------------------------------
    # An ingest run spends money, so it refuses to start during DeepSeek's peak
    # hours unless explicitly approved.  Defaults mirror the published windows:
    # 01:00-04:00 and 06:00-10:00 UTC, Mon-Fri; everything else is off-peak.
    allow_peak: bool = field(default_factory=lambda: _bool("READING_ALLOW_PEAK", False))
    peak_windows: str = field(
        default_factory=lambda: os.environ.get(
            "READING_PEAK_WINDOWS", "01:00-04:00,06:00-10:00"
        )
    )
    peak_weekdays: str = field(
        default_factory=lambda: os.environ.get(
            "READING_PEAK_WEEKDAYS", "Mon,Tue,Wed,Thu,Fri"
        )
    )
    # Comma-separated ISO dates known to be Chinese public holidays (off-peak all day).
    offpeak_dates: str = field(
        default_factory=lambda: os.environ.get("READING_OFFPEAK_DATES", "")
    )

    def peak_policy(self):
        """The resolved :class:`reading.pipeline.offpeak.PeakPolicy`."""
        from reading.pipeline.offpeak import PeakPolicy, parse_weekdays, parse_windows

        return PeakPolicy(
            windows=parse_windows(self.peak_windows),
            weekdays=parse_weekdays(self.peak_weekdays),
            offpeak_dates=frozenset(
                part.strip() for part in self.offpeak_dates.split(",") if part.strip()
            ),
        )

    @property
    def cefr_levels(self) -> tuple[str, ...]:
        return ("A1", "A2", "B1", "B2", "C1", "C2")

    def require_api_key(self) -> str:
        """Return the API key or raise with an actionable message."""
        if not self.deepseek_api_key:
            raise RuntimeError(
                "DEEPSEEK_API_KEY is not set.  Copy reading/.env.example to "
                "reading/.env and fill it in, or export the variable."
            )
        return self.deepseek_api_key


def _split(raw: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in raw.split(",") if part.strip())


settings = Settings()

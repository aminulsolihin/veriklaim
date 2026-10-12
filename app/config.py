"""Settings, read once from environment variables."""
from __future__ import annotations

import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _db_url(raw: str) -> str:
    # Tolerate what people paste from a provider's "connect" dialog: surrounding quotes,
    # a "psql" command around the URL, or a DATABASE_URL= prefix.
    raw = " ".join(raw.split())  # a line break inside a pasted value
    for prefix in ("DATABASE_URL=", "DATABASE_URL =", "psql ", "jdbc:"):
        if raw.startswith(prefix):
            raw = raw[len(prefix):].strip()
    raw = raw.strip("'\"").strip()
    if "://" in raw:
        raw = "".join(raw.split())  # a URL has no spaces; drop any from a wrapped paste
    if raw and "://" not in raw and "host=" in raw:
        # libpq "host=... user=... dbname=..." form, which some dialogs show
        from urllib.parse import quote
        parts = dict(p.split("=", 1) for p in raw.split() if "=" in p)
        q = "&".join("{}={}".format(k, v) for k, v in parts.items() if k in ("sslmode", "channel_binding"))
        raw = "postgresql://{}:{}@{}{}/{}{}".format(
            quote(parts.get("user", ""), safe=""), quote(parts.get("password", ""), safe=""), parts.get("host", ""),
            ":" + parts["port"] if "port" in parts else "", parts.get("dbname", ""), "?" + q if q else "")
    # Hosts hand out postgres:// or postgresql:// URLs; SQLAlchemy needs the driver named.
    if raw.startswith("postgres://"):
        raw = "postgresql://" + raw[len("postgres://"):]
    if raw.startswith("postgresql://"):
        raw = "postgresql+psycopg://" + raw[len("postgresql://"):]
    return raw


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


class Settings:
    def __init__(self) -> None:
        self.env = os.getenv("APP_ENV", "development").strip().lower()
        self.production = self.env == "production"
        self.database_url = _db_url(os.getenv("DATABASE_URL", "").strip() or "sqlite:///./veriklaim.db")

        key = os.getenv("SECRET_KEY", "").strip()
        self.secret_key_generated = not key
        self.secret_key = key or secrets.token_hex(32)

        self.verifikator_password = os.getenv("VERIFIKATOR_PASSWORD", "")
        self.supervisor_password = os.getenv("SUPERVISOR_PASSWORD", "")
        # Optional text shown on the sign-in page, for example the demo accounts for judges.
        self.login_hint = os.getenv("LOGIN_HINT", "").strip()[:300]

        self.max_upload_bytes = _int("MAX_UPLOAD_MB", 8) * 1024 * 1024
        self.max_claims = _int("MAX_CLAIMS", 2000)
        self.min_claims = 20
        self.seed_sample = os.getenv("SEED_SAMPLE", "1").strip() != "0"
        self.session_hours = _int("SESSION_HOURS", 8)
        self.session_cookie = os.getenv("SESSION_COOKIE", "").strip() or "vk_session"

        # Judge mode: a "try without an account" button that gives each visitor a private copy of
        # the synthetic sample, removed automatically after GUEST_HOURS.
        self.judge_mode = os.getenv("JUDGE_MODE", "1").strip() != "0"
        self.guest_hours = _int("GUEST_HOURS", 24)
        self.max_guests = _int("MAX_GUESTS", 300)
        self.guest_logins_per_ip_hour = _int("GUEST_LOGINS_PER_IP_HOUR", 60)
        self.guest_max_batches = _int("GUEST_MAX_BATCHES", 5)

        self.static_dir = BASE_DIR / "static"
        self.data_dir = BASE_DIR / "data"


settings = Settings()

"""Settings, read once from environment variables."""
from __future__ import annotations

import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _db_url(raw: str) -> str:
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
        # Firebase Hosting passes only a cookie named __session through to Cloud Run.
        self.session_cookie = os.getenv("SESSION_COOKIE", "").strip() or "vk_session"

        self.static_dir = BASE_DIR / "static"
        self.data_dir = BASE_DIR / "data"


settings = Settings()

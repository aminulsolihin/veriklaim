"""Database engine and tables. SQLite by default, Postgres when DATABASE_URL points at one."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterator, Optional

from sqlalchemy import JSON, BigInteger, inspect, text, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from .config import settings

_sqlite = settings.database_url.startswith("sqlite")


def _check_url(url: str) -> None:
    """Stop with a plain message, not a traceback, when DATABASE_URL is not a database address."""
    from sqlalchemy.engine import make_url
    from sqlalchemy.exc import ArgumentError
    try:
        make_url(url)
    except ArgumentError:
        raise SystemExit(
            "DATABASE_URL is not a valid database address. Paste only the connection string from Neon, "
            "e.g. postgresql://user:password@ep-xxxx.aws.neon.tech/neondb?sslmode=require"
        ) from None


_check_url(settings.database_url)
engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False} if _sqlite else {},
    # Free Postgres hosts suspend idle databases; check a connection before using it.
    pool_pre_ping=True,
    pool_recycle=280,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is None:  # SQLite returns naive datetimes; they were stored as UTC
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    role: Mapped[str] = mapped_column(String(20))  # verifikator | supervisor
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ReferencePrice(Base):
    """Reference price of an item paid as a Special CMG top-up."""
    __tablename__ = "reference_prices"
    code: Mapped[str] = mapped_column(String(8), primary_key=True)  # e.g. RR04
    kind: Mapped[str] = mapped_column(String(2))  # SR = Special Prosthesis, SD = Special Drug
    description: Mapped[str] = mapped_column(String(120))
    price: Mapped[int] = mapped_column(BigInteger)
    source: Mapped[str] = mapped_column(String(120), default="")
    updated_by: Mapped[str] = mapped_column(String(64), default="system")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Batch(Base):
    """One uploaded E-Klaim file, after screening. The raw file is not kept."""
    __tablename__ = "batches"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    filename: Mapped[str] = mapped_column(String(200), default="")
    uploaded_by: Mapped[str] = mapped_column(String(64))
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    rows_total: Mapped[int] = mapped_column(Integer)
    rows_screened: Mapped[int] = mapped_column(Integer)
    skipped_no_topup: Mapped[int] = mapped_column(Integer, default=0)
    skipped_no_ref: Mapped[int] = mapped_column(Integer, default=0)
    skipped_bad: Mapped[int] = mapped_column(Integer, default=0)
    truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    threshold_pct: Mapped[int] = mapped_column(Integer, default=100)
    top_k: Mapped[int] = mapped_column(Integer, default=12)
    base_score: Mapped[float] = mapped_column(Float)
    labelled: Mapped[bool] = mapped_column(Boolean, default=False)  # synthetic sample with injected-pattern labels
    # None: a team batch. A username: the private batch of one judge-mode guest.
    owner: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)


class Claim(Base):
    """One screened claim (one SEP). Holds no patient name, MRN, birth date or raw card number."""
    __tablename__ = "claims"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("batches.id", ondelete="CASCADE"), index=True)
    pos: Mapped[int] = mapped_column(Integer)  # row order in the file; also the row of the model matrix
    sep: Mapped[str] = mapped_column(String(40), index=True)
    kode_rs: Mapped[str] = mapped_column(String(20))
    kelas_rs: Mapped[str] = mapped_column(String(8), default="")
    kode_tarif: Mapped[str] = mapped_column(String(8), default="")
    adm_date: Mapped[str] = mapped_column(String(20), default="")
    dis_date: Mapped[str] = mapped_column(String(20), default="")
    adm_day: Mapped[int] = mapped_column(Integer)  # proleptic ordinal, for the 90-day window
    inacbg: Mapped[str] = mapped_column(String(20), default="")
    inacbg_desc: Mapped[str] = mapped_column(String(200), default="")
    diag: Mapped[str] = mapped_column(String(200), default="")
    proc: Mapped[str] = mapped_column(String(200), default="")
    cmg_code: Mapped[str] = mapped_column(String(8))
    cmg_final: Mapped[str] = mapped_column(String(16), default="")
    cmg_kind: Mapped[str] = mapped_column(String(2))
    cmg_desc: Mapped[str] = mapped_column(String(120), default="")
    topup: Mapped[int] = mapped_column(BigInteger, default=0)
    tarif_inacbg: Mapped[int] = mapped_column(BigInteger, default=0)
    total_tarif: Mapped[int] = mapped_column(BigInteger, default=0)
    tarif_rs: Mapped[int] = mapped_column(BigInteger)
    los: Mapped[int] = mapped_column(Integer, default=0)
    card_token: Mapped[str] = mapped_column(String(24), default="")
    # features
    ref_price: Mapped[int] = mapped_column(BigInteger)
    cover: Mapped[float] = mapped_column(Float)
    los_median: Mapped[float] = mapped_column(Float)
    los_ratio: Mapped[float] = mapped_column(Float)
    repeat_n: Mapped[int] = mapped_column(Integer)
    vol_n: Mapped[int] = mapped_column(Integer)
    vol_ratio: Mapped[float] = mapped_column(Float)
    # model output
    score: Mapped[float] = mapped_column(Float)
    rank: Mapped[int] = mapped_column(Integer, index=True)
    phi: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)  # Shapley value per feature
    label: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)  # synthetic sample only
    # current review decision; the history is in the audit log
    review_status: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    review_note: Mapped[str] = mapped_column(Text, default="")
    rs_response: Mapped[str] = mapped_column(Text, default="")  # the hospital's reply to a document request
    reviewed_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class Feedback(Base):
    """What people who tried the app said about it: the evidence of use a later MVP claim needs."""
    __tablename__ = "feedback"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    username: Mapped[str] = mapped_column(String(64))
    account_role: Mapped[str] = mapped_column(String(20), default="")
    peran: Mapped[str] = mapped_column(String(40))  # how the person describes their own work
    institusi: Mapped[str] = mapped_column(String(120), default="")
    useful: Mapped[int] = mapped_column(Integer)  # 1 to 5
    would_use: Mapped[str] = mapped_column(String(10), default="")  # ya / mungkin / tidak
    comment: Mapped[str] = mapped_column(Text, default="")


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    username: Mapped[str] = mapped_column(String(64))
    role: Mapped[str] = mapped_column(String(20), default="")
    action: Mapped[str] = mapped_column(String(40))
    batch_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    sep: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    detail: Mapped[str] = mapped_column(Text, default="")


def add_missing_columns() -> None:
    """create_all does not add columns to an existing table. Add the few columns that later
    versions introduced, so a database made by an earlier version keeps working."""
    added = {"claims": {"rs_response": "TEXT DEFAULT ''"}, "batches": {"owner": "VARCHAR(64)"}}
    insp = inspect(engine)
    with engine.begin() as conn:
        tables = set(insp.get_table_names())
        for table, cols in added.items():
            if table not in tables:
                continue
            have = {c["name"] for c in insp.get_columns(table)}
            for name, ddl in cols.items():
                if name not in have:
                    conn.execute(text("ALTER TABLE {} ADD COLUMN {} {}".format(table, name, ddl)))


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

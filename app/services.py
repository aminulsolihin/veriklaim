"""Work shared by the API routes and the start-up seed: ingesting a file, explaining claims, audit entries."""
from __future__ import annotations

import json
import logging
import secrets
from datetime import timedelta
from typing import Optional

import numpy as np
from sqlalchemy import delete, func, inspect as sa_inspect, select
from sqlalchemy.orm import Session

from . import coding, eklaim, scoring
from .config import settings
from .db import AuditLog, Batch, Claim, CodingBatch, CodingClaim, ReferencePrice, User, utcnow
from .security import card_token, hash_password, verify_password

log = logging.getLogger("veriklaim")

# Placeholder reference prices so the sample can be screened. They are not catalogue data.
DEFAULT_REFERENCE_PRICES = [
    {"code": "RR04", "kind": "SR", "description": "Hip implant / knee implant", "price": 25_000_000,
     "source": "Contoh ilustratif (produksi: e-Katalog LKPP)"},
    {"code": "DD01", "kind": "SD", "description": "Streptokinase", "price": 4_000_000,
     "source": "Contoh ilustratif (produksi: e-Fornas / e-Katalog)"},
]
# Source strings written by earlier versions, refreshed on start-up when nobody has edited the row.
_OLD_SOURCES = {
    "RR04": "Placeholder (production: LKPP e-Katalog)",
    "DD01": "Placeholder (production: e-Fornas / e-Katalog)",
}
DEMO_USERS = [
    {"username": "verifikator", "name": "Verifikator (demo)", "role": "verifikator", "env": "verifikator_password"},
    {"username": "supervisor", "name": "Supervisor (demo)", "role": "supervisor", "env": "supervisor_password"},
]


def audit(db: Session, user: Optional[User], action: str, *, batch_id: Optional[int] = None,
          sep: Optional[str] = None, detail: Optional[dict] = None, username: str = "") -> None:
    db.add(AuditLog(
        username=user.username if user else (username or "system"),
        role=user.role if user else "",
        action=action, batch_id=batch_id, sep=sep,
        detail=json.dumps(detail, ensure_ascii=False) if detail else "",
    ))


def reference_map(db: Session) -> dict[str, dict]:
    return {
        r.code: {"kind": r.kind, "description": r.description, "price": r.price}
        for r in db.scalars(select(ReferencePrice))
    }


def ingest(db: Session, text: str, *, name: str, filename: str, user: Optional[User],
           labels: Optional[dict] = None, owner: Optional[str] = None) -> Batch:
    """Parse, screen and store one E-Klaim file. Raises eklaim.EklaimError with a message for the user."""
    parsed = eklaim.parse_eklaim(text, reference_map(db), card_token)
    rows = parsed["rows"]
    if len(rows) < settings.min_claims:
        raise eklaim.EklaimError(
            "Hanya {} klaim di berkas ini yang memiliki top-up Special Drug atau Special Prosthesis dengan "
            "harga acuan. Penyaringan membutuhkan minimal {} klaim.".format(len(rows), settings.min_claims)
        )
    truncated = len(rows) > settings.max_claims
    if truncated:
        rows = rows[: settings.max_claims]
    model = scoring.screen(rows)

    batch = Batch(
        name=name[:160], filename=filename[:200], uploaded_by=user.username if user else "system",
        rows_total=parsed["total"], rows_screened=len(rows), skipped_no_topup=parsed["no_topup"],
        skipped_no_ref=parsed["no_ref"], skipped_bad=parsed["bad"], truncated=truncated,
        threshold_pct=100, top_k=scoring.default_top_k(len(rows)), base_score=model.base, labelled=bool(labels),
        owner=owner,
    )
    db.add(batch)
    db.flush()
    db.add_all([
        Claim(batch_id=batch.id, label=(labels or {}).get(r["sep"]), **r) for r in rows
    ])
    audit(db, user, "batch_upload", batch_id=batch.id, detail={
        "name": batch.name, "rows": parsed["total"], "screened": len(rows),
        "no_topup": parsed["no_topup"], "no_reference_price": parsed["no_ref"], "unreadable": parsed["bad"],
    })
    db.commit()
    scoring.remember(batch.id, model)
    return batch


def _matrix(db: Session, batch_id: int) -> np.ndarray:
    rows = db.execute(
        select(Claim.cover, Claim.los_ratio, Claim.repeat_n, Claim.vol_ratio)
        .where(Claim.batch_id == batch_id).order_by(Claim.pos)
    ).all()
    return np.array([scoring.feature_vector(*r) for r in rows], dtype=float)


def ensure_phi(db: Session, claim: Claim) -> list[float]:
    """Shapley values for one claim, computed on first use and then stored."""
    if claim.phi is not None:
        return claim.phi
    model = scoring.model_for(claim.batch_id, lambda: _matrix(db, claim.batch_id))
    claim.phi = [float(x) for x in model.shapley([claim.pos])[0]]
    db.commit()
    return claim.phi


def delete_batch(db: Session, batch: Batch) -> None:
    db.execute(delete(Claim).where(Claim.batch_id == batch.id))
    db.delete(batch)
    scoring.forget(batch.id)


def seed(db: Session) -> None:
    """Create the two accounts, the placeholder reference prices and the synthetic sample batch."""
    for spec in DEMO_USERS:
        password = getattr(settings, spec["env"])
        user = db.scalar(select(User).where(User.username == spec["username"]))
        if user is None:
            generated = not password
            password = password or secrets.token_urlsafe(9)
            db.add(User(username=spec["username"], name=spec["name"], role=spec["role"],
                        password_hash=hash_password(password)))
            if generated:
                log.warning("Created account '%s' with a generated password: %s  (set %s to choose your own)",
                            spec["username"], password, spec["env"].upper())
        elif password:
            # The environment variable is the source of truth for the demo accounts.
            if not verify_password(password, user.password_hash):
                user.password_hash = hash_password(password)
    if db.scalar(select(func.count()).select_from(ReferencePrice)) == 0:
        db.add_all([ReferencePrice(**r) for r in DEFAULT_REFERENCE_PRICES])
    else:
        for r in DEFAULT_REFERENCE_PRICES:
            row = db.get(ReferencePrice, r["code"])
            if row is not None and row.updated_by == "system" and row.source == _OLD_SOURCES.get(r["code"]):
                row.source = r["source"]
    db.commit()

    team_batches = db.scalar(select(func.count()).select_from(Batch).where(Batch.owner.is_(None)))
    if settings.seed_sample and team_batches == 0:
        clone_batch(db, _template_batch(db), owner=None, uploaded_by="system")
        db.commit()
    team_coding = db.scalar(select(func.count()).select_from(CodingBatch).where(CodingBatch.owner.is_(None)))
    if settings.seed_sample and team_coding == 0:
        clone_coding_batch(db, _coding_template(db), owner=None, uploaded_by="system")
        db.commit()
    if settings.judge_mode:
        _template_batch(db)
        _coding_template(db)
        purge_guests(db)


# ---------- judge mode ----------

GUEST_ROLE = "juri"
SAMPLE_NAME = "Sampel sintetis (80 klaim)"
SAMPLE_FILE = "veriklaim-sample-eklaim.txt"
_REVIEW_FIELDS = {"review_status", "review_note", "reviewed_by", "reviewed_at", "rs_response"}


def _template_batch(db: Session) -> Batch:
    """The scored synthetic sample that guests get a copy of. Kept as a hidden batch owned by
    'system:template', so team members deleting their own sample does not affect judge mode."""
    batch = db.scalar(select(Batch).where(Batch.owner == "system:template"))
    if batch is None:
        text = (settings.data_dir / "sample-eklaim.txt").read_text(encoding="utf-8")
        labels = json.loads((settings.data_dir / "sample-labels.json").read_text(encoding="utf-8"))
        batch = ingest(db, text, name=SAMPLE_NAME, filename=SAMPLE_FILE, user=None, labels=labels,
                       owner="system:template")
    return batch


def clone_batch(db: Session, source: Batch, *, owner: str, uploaded_by: str) -> Batch:
    """Copy a scored batch without re-running the model, leaving out every review decision."""
    cols = [c.key for c in sa_inspect(Batch).mapper.column_attrs if c.key not in ("id", "owner", "uploaded_by", "uploaded_at")]
    batch = Batch(**{k: getattr(source, k) for k in cols}, owner=owner, uploaded_by=uploaded_by)
    db.add(batch)
    db.flush()
    ccols = [c.key for c in sa_inspect(Claim).mapper.column_attrs if c.key not in {"id", "batch_id"} | _REVIEW_FIELDS]
    claims = db.scalars(select(Claim).where(Claim.batch_id == source.id)).all()
    db.add_all([Claim(batch_id=batch.id, **{k: getattr(c, k) for k in ccols}) for c in claims])
    return batch


def create_guest(db: Session) -> User:
    """A judge-mode visitor: an account without a password and a private copy of the sample."""
    template = _template_batch(db)
    username = "juri-" + secrets.token_hex(3)
    while db.scalar(select(User.id).where(User.username == username)) is not None:
        username = "juri-" + secrets.token_hex(3)
    user = User(username=username, name="Juri (tamu)", role=GUEST_ROLE, password_hash="!")  # no password can match
    db.add(user)
    db.flush()
    batch = clone_batch(db, template, owner=username, uploaded_by="system")
    clone_coding_batch(db, _coding_template(db), owner=username, uploaded_by="system")
    audit(db, user, "demo_login", batch_id=batch.id)
    db.commit()
    return user


def guest_expiry(user: User):
    created = user.created_at if user.created_at.tzinfo else user.created_at.replace(tzinfo=utcnow().tzinfo)
    return created + timedelta(hours=settings.guest_hours)


def purge_guests(db: Session) -> int:
    """Remove judge-mode accounts older than GUEST_HOURS with their batches. The audit log and the
    feedback they left are kept."""
    cutoff = utcnow() - timedelta(hours=settings.guest_hours)
    old = db.scalars(select(User).where(User.role == GUEST_ROLE, User.created_at < cutoff)).all()
    for user in old:
        for batch in db.scalars(select(Batch).where(Batch.owner == user.username)).all():
            delete_batch(db, batch)
        for cbatch in db.scalars(select(CodingBatch).where(CodingBatch.owner == user.username)).all():
            delete_coding_batch(db, cbatch)
        db.delete(user)
    if old:
        db.commit()
    return len(old)


def count_guests(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(User).where(User.role == GUEST_ROLE)) or 0


# ---------- upcoding check ----------

CODING_SAMPLE_NAME = "Sampel sintetis koding (1.087 klaim)"
CODING_SAMPLE_FILE = "veriklaim-sample-koding.txt"
_CODING_KEY = -1  # model cache key = _CODING_KEY * batch id, so it never collides with a top-up batch


def _coding_claim_args(r: dict) -> dict:
    out = {k: v for k, v in r.items() if k not in ("f", "ext")}
    out["f1"], out["f2"], out["f3"], out["f4"] = r["f"]
    return out


def ingest_coding(db: Session, text: str, *, name: str, filename: str, user: Optional[User],
                  labels: Optional[dict] = None, roles: Optional[dict] = None, owner: Optional[str] = None) -> CodingBatch:
    """Parse and check one E-Klaim file for upcoding indicators. Raises eklaim.EklaimError for the user."""
    parsed = coding.parse_coding(text)
    rows = parsed["rows"]
    if len(rows) < coding.MIN_ROWS:
        raise eklaim.EklaimError(
            "Hanya {} klaim di berkas ini yang dapat dibaca (kolom SEP dan INACBG berformat benar). "
            "Pemeriksaan koding membutuhkan minimal {} klaim, karena klaim dibandingkan dengan kelompok kasus yang sama "
            "di RS lain.".format(len(rows), coding.MIN_ROWS)
        )
    truncated = len(rows) > settings.max_claims
    if truncated:
        rows = rows[: settings.max_claims]
    model, info = coding.screen(rows)
    if info["groups"] == 0:
        raise eklaim.EklaimError(
            "Tidak ada kelompok kasus INA-CBG dengan cukup klaim di dua tingkat keparahan atau lebih untuk dibandingkan. "
            "Gunakan berkas yang lebih besar, misalnya satu bulan klaim rawat inap dari beberapa RS."
        )
    hospitals = info["hospitals"]
    for h in hospitals:
        h.pop("queued", None)  # depends on the capacity setting; the page counts it
        h["label"] = (roles or {}).get(h["rs"])
    batch = CodingBatch(
        name=name[:160], filename=filename[:200], uploaded_by=user.username if user else "system",
        rows_total=parsed["total"], rows_read=len(rows), skipped_bad=parsed["bad"], truncated=truncated,
        groups=info["groups"], compared=sum(1 for r in rows if r["tested"] and r["sev"] >= 2),
        hospitals_n=len({r["kode_rs"] for r in rows}), top_k=coding.default_top_k(info["high"]),
        base_score=model.base, labelled=bool(labels), hospitals=hospitals, owner=owner,
    )
    db.add(batch)
    db.flush()
    db.add_all([CodingClaim(batch_id=batch.id, label=(labels or {}).get(r["sep"]), **_coding_claim_args(r)) for r in rows])
    audit(db, user, "coding_upload", batch_id=batch.id, detail={
        "name": batch.name, "rows": parsed["total"], "read": len(rows), "unreadable": parsed["bad"],
        "compared": batch.compared, "modul": "koding",
    })
    db.commit()
    scoring.remember(_CODING_KEY * batch.id, model)
    return batch


def _coding_matrix(db: Session, batch_id: int) -> np.ndarray:
    rows = db.execute(
        select(CodingClaim.f1, CodingClaim.f2, CodingClaim.f3, CodingClaim.f4)
        .where(CodingClaim.batch_id == batch_id).order_by(CodingClaim.pos)
    ).all()
    return np.array([list(r) for r in rows], dtype=float)


def ensure_coding_phi(db: Session, claim: CodingClaim) -> Optional[list[float]]:
    """Shapley values for one claim, computed on first use and then stored. A claim that looks fully
    supported has all-zero features and nothing to explain."""
    if claim.phi is not None or not claim.live:
        return claim.phi
    model = scoring.model_for(_CODING_KEY * claim.batch_id, lambda: _coding_matrix(db, claim.batch_id))
    claim.phi = [float(x) for x in model.shapley([claim.pos])[0]]
    db.commit()
    return claim.phi


def delete_coding_batch(db: Session, batch: CodingBatch) -> None:
    db.execute(delete(CodingClaim).where(CodingClaim.batch_id == batch.id))
    db.delete(batch)
    scoring.forget(_CODING_KEY * batch.id)


def _coding_template(db: Session) -> CodingBatch:
    """The scored synthetic coding sample that guests and the team workspace start from."""
    batch = db.scalar(select(CodingBatch).where(CodingBatch.owner == "system:template"))
    if batch is None:
        text = (settings.data_dir / "sample-coding.txt").read_text(encoding="utf-8")
        key = json.loads((settings.data_dir / "sample-coding-labels.json").read_text(encoding="utf-8"))
        batch = ingest_coding(db, text, name=CODING_SAMPLE_NAME, filename=CODING_SAMPLE_FILE, user=None,
                              labels=key["claims"], roles=key["hospitals"], owner="system:template")
    return batch


def clone_coding_batch(db: Session, source: CodingBatch, *, owner: Optional[str], uploaded_by: str) -> CodingBatch:
    cols = [c.key for c in sa_inspect(CodingBatch).mapper.column_attrs if c.key not in ("id", "owner", "uploaded_by", "uploaded_at")]
    batch = CodingBatch(**{k: getattr(source, k) for k in cols}, owner=owner, uploaded_by=uploaded_by)
    db.add(batch)
    db.flush()
    ccols = [c.key for c in sa_inspect(CodingClaim).mapper.column_attrs if c.key not in {"id", "batch_id"} | _REVIEW_FIELDS]
    claims = db.scalars(select(CodingClaim).where(CodingClaim.batch_id == source.id)).all()
    db.add_all([CodingClaim(batch_id=batch.id, **{k: getattr(c, k) for k in ccols}) for c in claims])
    return batch

"""Work shared by the API routes and the start-up seed: ingesting a file, explaining claims, audit entries."""
from __future__ import annotations

import json
import logging
import secrets
from typing import Optional

import numpy as np
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from . import eklaim, scoring
from .config import settings
from .db import AuditLog, Batch, Claim, ReferencePrice, User, utcnow
from .security import card_token, hash_password, verify_password

log = logging.getLogger("veriklaim")

# Placeholder reference prices so the sample can be screened. They are not catalogue data.
DEFAULT_REFERENCE_PRICES = [
    {"code": "RR04", "kind": "SR", "description": "Hip implant / knee implant", "price": 25_000_000,
     "source": "Placeholder (production: LKPP e-Katalog)"},
    {"code": "DD01", "kind": "SD", "description": "Streptokinase", "price": 4_000_000,
     "source": "Placeholder (production: e-Fornas / e-Katalog)"},
]
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
           labels: Optional[dict] = None) -> Batch:
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
    db.commit()

    if settings.seed_sample and db.scalar(select(func.count()).select_from(Batch)) == 0:
        text = (settings.data_dir / "sample-eklaim.txt").read_text(encoding="utf-8")
        labels = json.loads((settings.data_dir / "sample-labels.json").read_text(encoding="utf-8"))
        ingest(db, text, name="Sampel sintetis (80 klaim)", filename="veriklaim-sample-eklaim.txt",
               user=None, labels=labels)

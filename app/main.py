"""VeriKlaim API and web app."""
from __future__ import annotations

import csv
import io
import logging
import os
import re
from contextlib import asynccontextmanager
from typing import Literal, Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from . import db as db_module, eklaim, scoring, services
from .config import settings
from .db import AuditLog, Base, Batch, Claim, ReferencePrice, SessionLocal, User, engine, get_db, iso, utcnow
from .security import DUMMY_HASH, login_limiter, verify_password

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("veriklaim")

CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "veriklaim"
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; form-action 'self'; "
    "base-uri 'none'; frame-ancestors 'none'"
)
REVIEW_STATUSES = ("confirmed", "cleared", "docs_requested")


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(engine)
    db_module.add_missing_columns()
    with SessionLocal() as db:
        services.seed(db)
    if settings.secret_key_generated:
        log.warning("SECRET_KEY is not set. A temporary key is in use: sign-ins end and card tokens change "
                    "when the server restarts.")
    yield


app = FastAPI(
    title="VeriKlaim", version="0.3.0", lifespan=lifespan,
    docs_url=None if settings.production else "/api/docs", redoc_url=None,
    openapi_url=None if settings.production else "/api/openapi.json",
)
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.add_middleware(
    SessionMiddleware, secret_key=settings.secret_key, session_cookie=settings.session_cookie,
    max_age=settings.session_hours * 3600, same_site="lax", https_only=settings.production,
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    if not request.url.path.startswith("/api/docs"):
        response.headers.setdefault("Content-Security-Policy", CSP)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-Robots-Tag", "noindex")
    if request.url.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


# ---------- dependencies ----------

def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    uid = request.session.get("uid")
    user = db.get(User, uid) if uid else None
    if user is None:
        raise HTTPException(401, "Silakan masuk terlebih dahulu.")
    return user


def supervisor(user: User = Depends(current_user)) -> User:
    if user.role != "supervisor":
        raise HTTPException(403, "Hanya supervisor yang dapat melakukan ini.")
    return user


def csrf(request: Request) -> None:
    # Browsers do not let another site add this header without a CORS preflight, which this API never allows.
    if request.headers.get(CSRF_HEADER, "").lower() != CSRF_VALUE:
        raise HTTPException(403, "Header permintaan tidak lengkap.")


def get_batch(batch_id: int, db: Session) -> Batch:
    batch = db.get(Batch, batch_id)
    if batch is None:
        raise HTTPException(404, "Batch tidak ditemukan. Mungkin sudah dihapus.")
    return batch


# ---------- serialisers ----------

def user_out(u: User) -> dict:
    return {"username": u.username, "name": u.name, "role": u.role}


def batch_out(b: Batch, db: Session) -> dict:
    flagged = or_(Claim.rank <= b.top_k, Claim.cover * 100 < b.threshold_pct)
    counts = db.execute(
        select(func.count(), func.count(Claim.review_status))
        .where(Claim.batch_id == b.id, flagged)
    ).one()
    return {
        "id": b.id, "name": b.name, "filename": b.filename, "uploadedBy": b.uploaded_by, "uploadedAt": iso(b.uploaded_at),
        "rowsTotal": b.rows_total, "rowsScreened": b.rows_screened, "noTopup": b.skipped_no_topup,
        "noRef": b.skipped_no_ref, "bad": b.skipped_bad, "truncated": b.truncated,
        "thresholdPct": b.threshold_pct, "topK": b.top_k, "maxTopK": scoring.max_top_k(b.rows_screened),
        "baseScore": b.base_score, "labelled": b.labelled, "flagged": counts[0], "reviewed": counts[1],
    }


def review_out(c: Claim) -> Optional[dict]:
    if not c.review_status:
        return None
    return {"status": c.review_status, "note": c.review_note, "by": c.reviewed_by, "at": iso(c.reviewed_at),
            "rsResponse": c.rs_response or ""}


def claim_out(c: Claim, full: bool = False) -> dict:
    out = {
        "id": c.id, "sep": c.sep, "rs": c.kode_rs, "kelasRs": c.kelas_rs, "kodeTarif": c.kode_tarif,
        "admTxt": c.adm_date, "disTxt": c.dis_date, "cbg": c.inacbg, "code": c.cmg_code, "final": c.cmg_final,
        "kind": c.cmg_kind, "item": c.cmg_desc, "topup": c.topup, "tarifRs": c.tarif_rs, "ref": c.ref_price,
        "cover": c.cover, "los": c.los, "losMed": c.los_median, "rep": c.repeat_n, "volN": c.vol_n,
        "vol": c.vol_ratio, "score": c.score, "rank": c.rank, "phi": c.phi, "label": c.label,
        "review": review_out(c),
    }
    if full:
        out.update({"cbgDesc": c.inacbg_desc, "diag": c.diag, "proc": c.proc, "total": c.total_tarif,
                    "tarifCbg": c.tarif_inacbg, "token": c.card_token})
    return out


# ---------- public ----------

@app.get("/healthz", include_in_schema=False)
def healthz(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return {"ok": True}


@app.get("/api/config")
def config():
    return {
        "loginHint": settings.login_hint,
        "maxUploadMb": settings.max_upload_bytes // (1024 * 1024),
        "maxClaims": settings.max_claims,
        "minClaims": settings.min_claims,
        "features": scoring.FEATURES,
    }


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=200)


@app.post("/api/login", dependencies=[Depends(csrf)])
def login(body: LoginIn, request: Request, db: Session = Depends(get_db)):
    username = body.username.strip().lower()
    key = "{}|{}".format(request.client.host if request.client else "?", username)
    if login_limiter.blocked(key):
        raise HTTPException(429, "Terlalu banyak percobaan masuk yang gagal. Tunggu sepuluh menit, lalu coba lagi.")
    user = db.scalar(select(User).where(User.username == username))
    ok = verify_password(body.password, user.password_hash if user else DUMMY_HASH)
    if not user or not ok:
        login_limiter.fail(key)
        services.audit(db, None, "login_failed", username=username[:64])
        db.commit()
        raise HTTPException(401, "Nama pengguna dan kata sandi tidak cocok.")
    login_limiter.reset(key)
    request.session.clear()
    request.session["uid"] = user.id
    services.audit(db, user, "login")
    db.commit()
    return user_out(user)


@app.post("/api/logout", dependencies=[Depends(csrf)])
def logout(request: Request, db: Session = Depends(get_db)):
    uid = request.session.get("uid")
    user = db.get(User, uid) if uid else None
    if user:
        services.audit(db, user, "logout")
        db.commit()
    request.session.clear()
    return {"ok": True}


@app.get("/api/me")
def me(user: User = Depends(current_user)):
    return user_out(user)


# ---------- batches ----------

@app.get("/api/batches")
def list_batches(user: User = Depends(current_user), db: Session = Depends(get_db)):
    batches = db.scalars(select(Batch).order_by(Batch.id.desc())).all()
    return [batch_out(b, db) for b in batches]


@app.post("/api/batches", dependencies=[Depends(csrf)])
def upload_batch(
    file: UploadFile = File(...), name: str = Form(""),
    user: User = Depends(current_user), db: Session = Depends(get_db),
):
    raw = file.file.read(settings.max_upload_bytes + 1)
    if len(raw) > settings.max_upload_bytes:
        raise HTTPException(413, "Berkas lebih besar dari {} MB, batas untuk prototype ini.".format(
            settings.max_upload_bytes // (1024 * 1024)))
    filename = os.path.basename(file.filename or "upload.txt")
    label = name.strip() or re.sub(r"\.[A-Za-z0-9]{1,5}$", "", filename) or "Batch tanpa nama"
    try:
        batch = services.ingest(db, eklaim.decode(raw), name=label, filename=filename, user=user)
    except eklaim.EklaimError as exc:
        raise HTTPException(400, str(exc)) from None
    return batch_out(batch, db)


@app.get("/api/batches/{batch_id}")
def batch_detail(batch_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    batch = get_batch(batch_id, db)
    claims = db.scalars(select(Claim).where(Claim.batch_id == batch_id).order_by(Claim.rank)).all()
    return {"batch": batch_out(batch, db), "claims": [claim_out(c) for c in claims]}


class SettingsIn(BaseModel):
    thresholdPct: int = Field(ge=50, le=200)
    topK: int = Field(ge=1, le=scoring.EAGER_EXPLAIN)


@app.patch("/api/batches/{batch_id}", dependencies=[Depends(csrf)])
def batch_settings(batch_id: int, body: SettingsIn, user: User = Depends(supervisor), db: Session = Depends(get_db)):
    batch = get_batch(batch_id, db)
    top_k = min(body.topK, scoring.max_top_k(batch.rows_screened))
    before = {"thresholdPct": batch.threshold_pct, "topK": batch.top_k}
    batch.threshold_pct, batch.top_k = body.thresholdPct, top_k
    services.audit(db, user, "batch_settings", batch_id=batch.id,
                   detail={"from": before, "to": {"thresholdPct": batch.threshold_pct, "topK": batch.top_k}})
    db.commit()
    return batch_out(batch, db)


@app.delete("/api/batches/{batch_id}", dependencies=[Depends(csrf)])
def batch_delete(batch_id: int, user: User = Depends(supervisor), db: Session = Depends(get_db)):
    batch = get_batch(batch_id, db)
    services.audit(db, user, "batch_delete", batch_id=batch.id, detail={"name": batch.name})
    services.delete_batch(db, batch)
    db.commit()
    return {"ok": True}


def _csv_cell(value) -> str:
    """Neutralise cells a spreadsheet would run as a formula."""
    s = "" if value is None else str(value)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


@app.get("/api/batches/{batch_id}/export.csv")
def batch_export(
    batch_id: int, view: Literal["flagged", "all"] = "flagged",
    thr: Optional[int] = Query(None, ge=50, le=200), k: Optional[int] = Query(None, ge=1, le=scoring.EAGER_EXPLAIN),
    user: User = Depends(current_user), db: Session = Depends(get_db),
):
    batch = get_batch(batch_id, db)
    thr = thr if thr is not None else batch.threshold_pct
    k = k if k is not None else batch.top_k
    claims = db.scalars(select(Claim).where(Claim.batch_id == batch_id).order_by(Claim.rank)).all()
    drivers = ["Hospital bill", "Length of stay", "Repeat top-up", "Top-up volume"]
    buf = io.StringIO()
    w = csv.writer(buf, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
    w.writerow(["SEP", "KODE_RS", "KELAS_RS", "KODE_TARIF", "INACBG", "SPECIAL_CMG", "TARIF_RS", "REFERENCE_PRICE",
                "BILL_TO_REFERENCE", "LOS", "LOS_MEDIAN_SAME_CBG", "SAME_TOPUP_SAME_CARD_90D", "VOLUME_VS_PEERS",
                "ANOMALY_SCORE", "RANK", "BILL_RULE", "ANOMALY_LAYER", "MAIN_DRIVER", "PHI_BILL", "PHI_LOS",
                "PHI_REPEAT", "PHI_VOLUME", "CARD_TOKEN", "REVIEW_STATUS", "REVIEW_NOTE", "RS_RESPONSE", "REVIEWED_BY",
                "REVIEWED_AT"])
    n = 0
    for c in claims:
        rule, model = c.cover * 100 < thr - 1e-9, c.rank <= k
        if view == "flagged" and not (rule or model):
            continue
        phi = c.phi or [None] * 4
        driver = drivers[max(range(4), key=lambda i: phi[i])] if (model and c.phi) else ("Hospital bill" if rule else "")
        w.writerow([_csv_cell(v) for v in [
            c.sep, c.kode_rs, c.kelas_rs, c.kode_tarif, c.inacbg, c.cmg_final or c.cmg_code, c.tarif_rs, c.ref_price,
            "%.3f" % c.cover, c.los, "%g" % c.los_median, c.repeat_n, "%.3f" % c.vol_ratio, "%.4f" % c.score, c.rank,
            "flagged" if rule else "pass", "flagged" if model else "pass", driver,
            *["" if x is None else "%.4f" % x for x in phi],
            c.card_token, c.review_status or "", c.review_note, c.rs_response or "", c.reviewed_by or "", iso(c.reviewed_at) or "",
        ]])
        n += 1
    services.audit(db, user, "export", batch_id=batch.id, detail={"view": view, "rows": n, "thresholdPct": thr, "topK": k})
    db.commit()
    return Response(
        "﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="veriklaim-batch-{}-{}.csv"'.format(batch.id, view)},
    )


# ---------- claims ----------

def get_claim(claim_id: int, db: Session) -> Claim:
    claim = db.get(Claim, claim_id)
    if claim is None:
        raise HTTPException(404, "Klaim tidak ditemukan. Batch-nya mungkin sudah dihapus.")
    return claim


@app.get("/api/claims/{claim_id}")
def claim_detail(claim_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    claim = get_claim(claim_id, db)
    services.ensure_phi(db, claim)
    return claim_out(claim, full=True)


class ReviewIn(BaseModel):
    status: Optional[Literal["confirmed", "cleared", "docs_requested"]] = None  # None reopens the claim
    note: str = Field("", max_length=1000)
    rsResponse: str = Field("", max_length=1000)  # what the hospital sent back, when documents were requested


@app.put("/api/claims/{claim_id}/review", dependencies=[Depends(csrf)])
def claim_review(claim_id: int, body: ReviewIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    claim = get_claim(claim_id, db)
    before = claim.review_status
    note, response = body.note.strip(), body.rsResponse.strip()
    if body.status is None:
        claim.review_status, claim.review_note, claim.reviewed_by, claim.reviewed_at = None, "", None, None
        claim.rs_response = ""
    else:
        claim.review_status, claim.review_note = body.status, note
        claim.reviewed_by, claim.reviewed_at = user.username, utcnow()
        claim.rs_response = response
    detail = {"from": before, "to": body.status, "note": note[:200]}
    if response:
        detail["rsResponse"] = response[:200]
    services.audit(db, user, "review_set" if body.status else "review_reopened", batch_id=claim.batch_id,
                   sep=claim.sep, detail=detail)
    db.commit()
    return claim_out(claim, full=True)


# ---------- reference prices ----------

def ref_out(r: ReferencePrice) -> dict:
    return {"code": r.code, "kind": r.kind, "type": eklaim.KIND_NAMES.get(r.kind, r.kind),
            "description": r.description, "price": r.price, "source": r.source,
            "updatedBy": r.updated_by, "updatedAt": iso(r.updated_at)}


@app.get("/api/reference-prices")
def list_refs(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return [ref_out(r) for r in db.scalars(select(ReferencePrice).order_by(ReferencePrice.code))]


class RefIn(BaseModel):
    kind: Literal["SR", "SD"]
    description: str = Field(min_length=2, max_length=120)
    price: int = Field(gt=0, le=10 ** 12)
    source: str = Field("", max_length=120)


@app.put("/api/reference-prices/{code}", dependencies=[Depends(csrf)])
def set_ref(code: str, body: RefIn, user: User = Depends(supervisor), db: Session = Depends(get_db)):
    code = code.strip().upper()
    if not re.fullmatch(r"[A-Z]{2}\d{2}", code):
        raise HTTPException(400, "Kode Special CMG terdiri dari dua huruf dan dua angka, misalnya RR04.")
    ref = db.get(ReferencePrice, code)
    before = {"price": ref.price, "kind": ref.kind} if ref else None
    if ref is None:
        ref = ReferencePrice(code=code)
        db.add(ref)
    ref.kind, ref.description, ref.price = body.kind, body.description.strip(), body.price
    ref.source, ref.updated_by, ref.updated_at = body.source.strip(), user.username, utcnow()
    services.audit(db, user, "reference_price_set", detail={"code": code, "from": before,
                                                            "to": {"price": body.price, "kind": body.kind}})
    db.commit()
    return ref_out(ref)


@app.delete("/api/reference-prices/{code}", dependencies=[Depends(csrf)])
def delete_ref(code: str, user: User = Depends(supervisor), db: Session = Depends(get_db)):
    ref = db.get(ReferencePrice, code.strip().upper())
    if ref is None:
        raise HTTPException(404, "Harga acuan tidak ditemukan.")
    services.audit(db, user, "reference_price_delete", detail={"code": ref.code, "price": ref.price})
    db.delete(ref)
    db.commit()
    return {"ok": True}


# ---------- audit log ----------

@app.get("/api/audit")
def audit_log(limit: int = Query(200, ge=1, le=1000), user: User = Depends(supervisor), db: Session = Depends(get_db)):
    rows = db.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(limit)).all()
    return [{"id": a.id, "at": iso(a.at), "username": a.username, "role": a.role, "action": a.action,
             "batchId": a.batch_id, "sep": a.sep, "detail": a.detail} for a in rows]


@app.exception_handler(404)
async def not_found(request: Request, exc):
    detail = getattr(exc, "detail", None)
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": detail or "Tidak ditemukan."}, status_code=404)
    return FileResponse(settings.static_dir / "index.html", status_code=404, headers={"Cache-Control": "no-cache"})


# ---------- web app ----------

app.mount("/assets", StaticFiles(directory=settings.static_dir / "assets"), name="assets")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(settings.static_dir / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(settings.static_dir / "assets" / "favicon.svg", media_type="image/svg+xml")


@app.get("/robots.txt", include_in_schema=False)
def robots():
    return Response("User-agent: *\nDisallow: /\n", media_type="text/plain")

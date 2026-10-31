"""VeriKlaim API and web app."""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import timedelta
from functools import lru_cache
from typing import Literal, Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import and_, case, func, or_, select, text
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from . import ai_judge, baseline, coding, db as db_module, eklaim, scoring, services
from .config import settings
from .db import AuditLog, Base, Batch, Claim, CodingBatch, CodingClaim, Feedback, ReferencePrice, SessionLocal, User, engine, get_db, iso, utcnow
from .security import DUMMY_HASH, LoginLimiter, client_ip, login_limiter, verify_password

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
    try:
        Base.metadata.create_all(engine)
    except Exception as exc:  # the most common first-deploy problem; say so in plain words
        log.error("Cannot reach the database in DATABASE_URL (%s: %s). Check the connection string: it should "
                  "start with postgresql:// and end with sslmode=require, with no quotes or 'psql' around it.",
                  type(exc).__name__, str(exc).splitlines()[0][:300] if str(exc) else "")
        raise
    db_module.add_missing_columns()
    with SessionLocal() as db:
        services.seed(db)
    if settings.secret_key_generated:
        log.warning("SECRET_KEY is not set. A temporary key is in use: sign-ins end and card tokens change "
                    "when the server restarts.")
    yield


app = FastAPI(
    title="VeriKlaim", version="0.6.2", lifespan=lifespan,
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
    if user.role == services.GUEST_ROLE and services.guest_expiry(user) < utcnow():
        request.session.clear()
        raise HTTPException(401, "Sesi juri sudah berakhir. Mulai sesi baru dari halaman masuk.")
    return user


def supervisor(user: User = Depends(current_user)) -> User:
    if user.role != "supervisor":
        raise HTTPException(403, "Hanya supervisor yang dapat melakukan ini.")
    return user


def csrf(request: Request) -> None:
    # Browsers do not let another site add this header without a CORS preflight, which this API never allows.
    if request.headers.get(CSRF_HEADER, "").lower() != CSRF_VALUE:
        raise HTTPException(403, "Header permintaan tidak lengkap.")


def visible_owner(user: User) -> Optional[str]:
    """Team accounts see team batches (owner None); a judge-mode guest sees only their own."""
    return user.username if user.role == services.GUEST_ROLE else None


def get_batch(batch_id: int, db: Session, user: User) -> Batch:
    batch = db.get(Batch, batch_id)
    if batch is None or batch.owner != visible_owner(user):
        raise HTTPException(404, "Batch tidak ditemukan. Mungkin sudah dihapus.")
    return batch


def can_manage(user: User) -> bool:
    """Saving thresholds and deleting batches: a supervisor for team batches, a guest for their own."""
    return user.role in ("supervisor", services.GUEST_ROLE)


# ---------- serialisers ----------

def user_out(u: User) -> dict:
    out = {"username": u.username, "name": u.name, "role": u.role, "guest": u.role == services.GUEST_ROLE}
    if out["guest"]:
        out["expiresAt"] = iso(services.guest_expiry(u))
    return out


def in_queue(b: Batch, thr: Optional[int] = None, k: Optional[int] = None):
    """The review queue: the bill rule, the top k by anomaly score, every SEP already submitted in an earlier
    batch, and every stay that overlaps another stay of the same card. The last two do not depend on the settings."""
    thr = b.threshold_pct if thr is None else thr
    return or_(Claim.rank <= (b.top_k if k is None else k), Claim.cover * 100 < thr, Claim.hist_batch_id.is_not(None),
               Claim.overlap_n > 0)


def batch_out(b: Batch, db: Session) -> dict:
    counts = db.execute(
        select(func.count(), func.count(Claim.review_status))
        .where(Claim.batch_id == b.id, in_queue(b))
    ).one()
    hist = db.execute(
        select(func.count(Claim.hist_batch_id), func.coalesce(func.sum(case((Claim.hist_repeat_n > 0, 1), else_=0)), 0),
               func.coalesce(func.sum(case((Claim.overlap_n > 0, 1), else_=0)), 0), func.count(Claim.readmit_days))
        .where(Claim.batch_id == b.id)
    ).one()
    ignored = list(b.hist_ignored or [])
    names = dict(db.execute(select(Batch.id, Batch.name).where(Batch.id.in_(ignored))).all()) if ignored else {}
    return {
        "id": b.id, "name": b.name, "filename": b.filename, "uploadedBy": b.uploaded_by, "uploadedAt": iso(b.uploaded_at),
        "rowsTotal": b.rows_total, "rowsScreened": b.rows_screened, "noTopup": b.skipped_no_topup,
        "noRef": b.skipped_no_ref, "bad": b.skipped_bad, "truncated": b.truncated,
        "thresholdPct": b.threshold_pct, "topK": b.top_k, "maxTopK": scoring.max_top_k(b.rows_screened),
        "baseScore": b.base_score, "labelled": b.labelled, "own": b.owner is not None, "flagged": counts[0], "reviewed": counts[1],
        "histSep": hist[0], "histCard": int(hist[1] or 0), "histKeySkipped": b.hist_key_skipped or 0,
        "overlap": int(hist[2] or 0), "readmit": hist[3],
        "histReexport": [{"id": i, "name": names.get(i)} for i in ignored],
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
        "dx": c.dx3 or "", "repExp": c.repeat_exp or 0.0, "losNat": c.los_nat,
        "overlapN": c.overlap_n or 0, "readmitDays": c.readmit_days,
        "vol": c.vol_ratio, "score": c.score, "rank": c.rank, "phi": c.phi, "label": c.label,
        "histBatch": c.hist_batch_id, "histSepN": c.hist_sep_n or 0, "histRep": c.hist_repeat_n or 0,
        "review": review_out(c),
    }
    if full:
        out.update({"cbgDesc": c.inacbg_desc, "diag": c.diag, "proc": c.proc, "total": c.total_tarif,
                    "tarifCbg": c.tarif_inacbg, "token": c.card_token, "overlapWith": c.overlap_with or [],
                    "readmitWith": c.readmit_with, "readmitRate": c.readmit_rate})
    return out


# ---------- public ----------

@app.get("/healthz", include_in_schema=False)
def healthz():
    # No database query: keep-alive pings every few minutes must not keep a free Postgres awake.
    return {"ok": True}


@app.get("/readyz", include_in_schema=False)
def readyz(db: Session = Depends(get_db)):
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
        "dxBaseline": baseline.source(),
        "codingFeatures": coding.FEATURES,
        "judgeMode": settings.judge_mode,
        "guestHours": settings.guest_hours,
        "aiJudge": settings.ai_judge,
        "aiGuests": settings.ai_guests,
    }


@lru_cache(maxsize=1)
def _robustness() -> dict:
    out = json.loads((settings.data_dir / "robustness.json").read_text(encoding="utf-8"))
    for key, name in (("realData", "realdata.json"), ("dxEval", "dx_eval.json")):
        try:
            out[key] = json.loads((settings.data_dir / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            out[key] = None
    return out


@app.get("/api/robustness")
def robustness(user: User = Depends(current_user)):
    """Results of tools/stress_test.py and tools/benchmark.py (synthetic data), the record rules on the BPJS sample
    data (tools/build_realdata_eval.py) and the national-norm A/B (tools/eval_dx_baseline.py), for the Ketahanan page."""
    try:
        return _robustness()
    except (OSError, ValueError):
        raise HTTPException(404, "Data ketahanan tidak tersedia pada instalasi ini.")


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=200)


@app.post("/api/login", dependencies=[Depends(csrf)])
def login(body: LoginIn, request: Request, db: Session = Depends(get_db)):
    username = body.username.strip().lower()
    key = "{}|{}".format(client_ip(request), username)
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


guest_limiter = LoginLimiter(limit=settings.guest_logins_per_ip_hour, window=3600)


@app.post("/api/demo-login", dependencies=[Depends(csrf)])
def demo_login(request: Request, db: Session = Depends(get_db)):
    """Judge mode: sign in without an account and get a private copy of the synthetic sample."""
    if not settings.judge_mode:
        raise HTTPException(404, "Mode juri tidak aktif di server ini.")
    key = client_ip(request)
    if guest_limiter.blocked(key):
        raise HTTPException(429, "Terlalu banyak sesi juri dari jaringan ini. Coba lagi dalam satu jam.")
    services.purge_guests(db)
    if services.count_guests(db) >= settings.max_guests:
        raise HTTPException(503, "Mode juri sedang penuh. Coba lagi beberapa saat lagi.")
    guest_limiter.fail(key)  # counts sessions created, not failures
    user = services.create_guest(db)
    request.session.clear()
    request.session["uid"] = user.id
    return user_out(user)


# ---------- feedback and usage ----------

class FeedbackIn(BaseModel):
    peran: Literal["verifikator", "koder-rs", "klinisi", "manajemen-rs", "juri", "lainnya"]
    institusi: str = Field("", max_length=120)
    useful: int = Field(ge=1, le=5)
    wouldUse: Literal["ya", "mungkin", "tidak"]
    comment: str = Field("", max_length=1500)


def feedback_out(f: Feedback) -> dict:
    return {"id": f.id, "at": iso(f.at), "username": f.username, "accountRole": f.account_role, "peran": f.peran,
            "institusi": f.institusi, "useful": f.useful, "wouldUse": f.would_use, "comment": f.comment}


@app.post("/api/feedback", dependencies=[Depends(csrf)])
def send_feedback(body: FeedbackIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    n = db.scalar(select(func.count()).select_from(Feedback).where(Feedback.username == user.username))
    if n >= 5:
        raise HTTPException(429, "Terima kasih, umpan balik Anda sudah tercatat.")
    f = Feedback(username=user.username, account_role=user.role, peran=body.peran, institusi=body.institusi.strip(),
                 useful=body.useful, would_use=body.wouldUse, comment=body.comment.strip())
    db.add(f)
    services.audit(db, user, "feedback", detail={"peran": body.peran, "useful": body.useful})
    db.commit()
    return feedback_out(f)


@app.get("/api/feedback")
def list_feedback(user: User = Depends(supervisor), db: Session = Depends(get_db)):
    return [feedback_out(f) for f in db.scalars(select(Feedback).order_by(Feedback.id.desc()).limit(500))]


@app.get("/api/feedback.csv")
def feedback_csv(user: User = Depends(supervisor), db: Session = Depends(get_db)):
    buf = io.StringIO()
    w = csv.writer(buf, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
    w.writerow(["WAKTU", "PENGGUNA", "AKUN", "PERAN", "INSTITUSI", "KEGUNAAN_1_5", "AKAN_MEMAKAI", "KOMENTAR"])
    for f in db.scalars(select(Feedback).order_by(Feedback.id)):
        w.writerow([_csv_cell(v) for v in [iso(f.at), f.username, f.account_role, f.peran, f.institusi, f.useful,
                                            f.would_use, f.comment]])
    return Response("\ufeff" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="veriklaim-umpan-balik.csv"'})


@app.get("/api/usage")
def usage(user: User = Depends(supervisor), db: Session = Depends(get_db)):
    """How the app has been used, from the audit log: the numbers to quote as evidence of use."""
    week = utcnow() - timedelta(days=7)

    def count(*conds):
        return db.scalar(select(func.count()).select_from(AuditLog).where(*conds)) or 0

    guest = AuditLog.role == services.GUEST_ROLE
    people = db.scalar(select(func.count(func.distinct(AuditLog.username))).where(
        AuditLog.action.in_(["login", "demo_login"]))) or 0
    avg = db.scalar(select(func.avg(Feedback.useful)))
    return {
        "guestSessions": count(AuditLog.action == "demo_login"),
        "guestSessions7d": count(AuditLog.action == "demo_login", AuditLog.at >= week),
        "activeGuests": services.count_guests(db),
        "distinctAccounts": people,
        "decisions": count(AuditLog.action.in_(["review_set", "coding_review_set"])),
        "guestDecisions": count(AuditLog.action.in_(["review_set", "coding_review_set"]), guest),
        "uploads": count(AuditLog.action == "batch_upload", AuditLog.username != "system"),
        "exports": count(AuditLog.action == "export"),
        "feedback": db.scalar(select(func.count()).select_from(Feedback)) or 0,
        "avgUseful": round(float(avg), 2) if avg is not None else None,
    }


@app.get("/api/me")
def me(user: User = Depends(current_user)):
    return user_out(user)


# ---------- batches ----------

@app.get("/api/batches")
def list_batches(user: User = Depends(current_user), db: Session = Depends(get_db)):
    owner = visible_owner(user)
    q = select(Batch).where(Batch.owner.is_(None) if owner is None else Batch.owner == owner)
    batches = db.scalars(q.order_by(Batch.id.desc())).all()
    return [batch_out(b, db) for b in batches]


@app.post("/api/batches", dependencies=[Depends(csrf)])
def upload_batch(
    file: UploadFile = File(...), name: str = Form(""),
    user: User = Depends(current_user), db: Session = Depends(get_db),
):
    owner = visible_owner(user)
    if owner is not None:
        n = db.scalar(select(func.count()).select_from(Batch).where(Batch.owner == owner))
        if n >= settings.guest_max_batches:
            raise HTTPException(400, "Mode juri dibatasi {} batch. Hapus satu batch dulu.".format(settings.guest_max_batches))
    raw = file.file.read(settings.max_upload_bytes + 1)
    if len(raw) > settings.max_upload_bytes:
        raise HTTPException(413, "Berkas lebih besar dari {} MB, batas untuk prototype ini.".format(
            settings.max_upload_bytes // (1024 * 1024)))
    filename = os.path.basename(file.filename or "upload.txt")
    label = name.strip() or re.sub(r"\.[A-Za-z0-9]{1,5}$", "", filename) or "Batch tanpa nama"
    try:
        batch = services.ingest(db, eklaim.decode(raw), name=label, filename=filename, user=user, owner=owner)
    except eklaim.EklaimError as exc:
        raise HTTPException(400, str(exc)) from None
    return batch_out(batch, db)


@app.get("/api/batches/{batch_id}")
def batch_detail(batch_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    batch = get_batch(batch_id, db, user)
    claims = db.scalars(select(Claim).where(Claim.batch_id == batch_id).order_by(Claim.rank)).all()
    return {"batch": batch_out(batch, db), "claims": [claim_out(c) for c in claims]}


class SettingsIn(BaseModel):
    thresholdPct: int = Field(ge=50, le=200)
    topK: int = Field(ge=1, le=scoring.EAGER_EXPLAIN)


@app.patch("/api/batches/{batch_id}", dependencies=[Depends(csrf)])
def batch_settings(batch_id: int, body: SettingsIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    batch = get_batch(batch_id, db, user)
    if not can_manage(user):
        raise HTTPException(403, "Hanya supervisor yang dapat melakukan ini.")
    top_k = min(body.topK, scoring.max_top_k(batch.rows_screened))
    before = {"thresholdPct": batch.threshold_pct, "topK": batch.top_k}
    batch.threshold_pct, batch.top_k = body.thresholdPct, top_k
    services.audit(db, user, "batch_settings", batch_id=batch.id,
                   detail={"from": before, "to": {"thresholdPct": batch.threshold_pct, "topK": batch.top_k}})
    db.commit()
    return batch_out(batch, db)


@app.delete("/api/batches/{batch_id}", dependencies=[Depends(csrf)])
def batch_delete(batch_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    batch = get_batch(batch_id, db, user)
    if not can_manage(user):
        raise HTTPException(403, "Hanya supervisor yang dapat melakukan ini.")
    services.audit(db, user, "batch_delete", batch_id=batch.id, detail={"name": batch.name})
    services.delete_batch(db, batch)
    db.commit()
    return {"ok": True}


_PLAIN_NUMBER = re.compile(r"-?\d+(\.\d+)?")


def _csv_cell(value) -> str:
    """Neutralise cells a spreadsheet would run as a formula. A plain number such as -0.0123 stays a number."""
    s = "" if value is None else str(value)
    if _PLAIN_NUMBER.fullmatch(s):
        return s
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


@app.get("/api/batches/{batch_id}/export.csv")
def batch_export(
    batch_id: int, view: Literal["flagged", "all"] = "flagged",
    thr: Optional[int] = Query(None, ge=50, le=200), k: Optional[int] = Query(None, ge=1, le=scoring.EAGER_EXPLAIN),
    user: User = Depends(current_user), db: Session = Depends(get_db),
):
    batch = get_batch(batch_id, db, user)
    thr = thr if thr is not None else batch.threshold_pct
    k = k if k is not None else batch.top_k
    claims = db.scalars(select(Claim).where(Claim.batch_id == batch_id).order_by(Claim.rank)).all()
    drivers = ["Tagihan RS", "Lama rawat", "Top-up berulang", "Volume top-up"]
    buf = io.StringIO()
    w = csv.writer(buf, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
    w.writerow(["SEP", "KODE_RS", "KELAS_RS", "KODE_TARIF", "INACBG", "SPECIAL_CMG", "TARIF_RS", "REFERENCE_PRICE",
                "BILL_TO_REFERENCE", "LOS", "LOS_MEDIAN_SAME_CBG", "SAME_TOPUP_SAME_CARD_90D", "VOLUME_VS_PEERS",
                "ANOMALY_SCORE", "RANK", "BILL_RULE", "ANOMALY_LAYER", "MAIN_DRIVER", "PHI_BILL", "PHI_LOS",
                "PHI_REPEAT", "PHI_VOLUME", "CARD_TOKEN", "REVIEW_STATUS", "REVIEW_NOTE", "RS_RESPONSE", "REVIEWED_BY",
                "REVIEWED_AT", "SAME_SEP_EARLIER_BATCH", "SAME_SEP_EARLIER_BATCHES_N", "SAME_TOPUP_SAME_CARD_90D_EARLIER_BATCHES",
                "HISTORY_RULE", "PRIMARY_DX", "REPEAT_EXPECTED_FOR_DX", "LOS_NATIONAL_MEDIAN_DX",
                "OVERLAPPING_STAYS_SAME_CARD", "OVERLAP_RULE", "OVERLAPPING_SEPS", "READMITTED_DAYS_AFTER_SAME_DX_STAY",
                "READMIT_NATIONAL_RATE_DX"])
    n = 0
    for c in claims:
        rule, model, hist = c.cover * 100 < thr - 1e-9, c.rank <= k, c.hist_batch_id is not None
        overlap = (c.overlap_n or 0) > 0
        if view == "flagged" and not (rule or model or hist or overlap):
            continue
        phi = c.phi or [None] * 4
        driver = (drivers[max(range(4), key=lambda i: phi[i])] if (model and c.phi)
                  else "Tagihan RS" if rule else "SEP sudah diajukan" if hist else "Rawat tumpang tindih" if overlap else "")
        w.writerow([_csv_cell(v) for v in [
            c.sep, c.kode_rs, c.kelas_rs, c.kode_tarif, c.inacbg, c.cmg_final or c.cmg_code, c.tarif_rs, c.ref_price,
            "%.3f" % c.cover, c.los, "%g" % c.los_median, c.repeat_n, "%.3f" % c.vol_ratio, "%.4f" % c.score, c.rank,
            "flagged" if rule else "pass", "flagged" if model else "pass", driver,
            *["" if x is None else "%.4f" % x for x in phi],
            c.card_token, c.review_status or "", c.review_note, c.rs_response or "", c.reviewed_by or "", iso(c.reviewed_at) or "",
            c.hist_batch_id or "", c.hist_sep_n or 0, c.hist_repeat_n or 0, "flagged" if hist else "pass",
            c.dx3 or "", "%g" % (c.repeat_exp or 0.0), "" if c.los_nat is None else "%g" % c.los_nat,
            c.overlap_n or 0, "flagged" if overlap else "pass", " ".join(o["sep"] for o in c.overlap_with or []),
            "" if c.readmit_days is None else c.readmit_days, "" if c.readmit_rate is None else "%.4f" % c.readmit_rate,
        ]])
        n += 1
    services.audit(db, user, "export", batch_id=batch.id, detail={"view": view, "rows": n, "thresholdPct": thr, "topK": k})
    db.commit()
    return Response(
        "﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="veriklaim-batch-{}-{}.csv"'.format(batch.id, view)},
    )


# ---------- claims ----------

def get_claim(claim_id: int, db: Session, user: User) -> Claim:
    claim = db.get(Claim, claim_id)
    batch = db.get(Batch, claim.batch_id) if claim is not None else None
    if claim is None or batch is None or batch.owner != visible_owner(user):
        raise HTTPException(404, "Klaim tidak ditemukan. Batch-nya mungkin sudah dihapus.")
    return claim


@app.get("/api/claims/{claim_id}")
def claim_detail(claim_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    claim = get_claim(claim_id, db, user)
    services.ensure_phi(db, claim)
    out = claim_out(claim, full=True)
    out["history"] = services.claim_history(db, claim, db.get(Batch, claim.batch_id))
    return out


class ReviewIn(BaseModel):
    status: Optional[Literal["confirmed", "cleared", "docs_requested"]] = None  # None reopens the claim
    note: str = Field("", max_length=1000)
    rsResponse: str = Field("", max_length=1000)  # what the hospital sent back, when documents were requested


@app.put("/api/claims/{claim_id}/review", dependencies=[Depends(csrf)])
def claim_review(claim_id: int, body: ReviewIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    claim = get_claim(claim_id, db, user)
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


@app.post("/api/claims/{claim_id}/ai-opinion", dependencies=[Depends(csrf)])
def claim_ai_opinion(claim_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Advice only: never saved as a review decision. The request and answer go to the audit log."""
    if user.role == services.GUEST_ROLE and not settings.ai_guests:
        raise HTTPException(403, "Pendapat AI tidak tersedia untuk akun tamu.")
    claim = get_claim(claim_id, db, user)
    services.ensure_phi(db, claim)
    try:
        out = ai_judge.opinion(db, claim, visible_owner(user))
    except ai_judge.AiJudgeError as e:
        raise HTTPException(503, str(e))
    services.audit(db, user, "ai_opinion", batch_id=claim.batch_id, sep=claim.sep,
                   detail={"recommendation": out.get("recommendation"), "confidence": out.get("confidence"),
                           "model": out.get("model"), "examples": out.get("examplesUsed")})
    db.commit()
    return out


# ---------- upcoding check ----------

def get_cbatch(batch_id: int, db: Session, user: User) -> CodingBatch:
    batch = db.get(CodingBatch, batch_id)
    if batch is None or batch.owner != visible_owner(user):
        raise HTTPException(404, "Batch tidak ditemukan. Mungkin sudah dihapus.")
    return batch


def _queued(b: CodingBatch, k: Optional[int] = None):
    """Claims in the queue at capacity `k` (the batch's saved capacity by default)."""
    return or_(CodingClaim.rule.is_(True), and_(CodingClaim.live.is_(True), CodingClaim.rank <= (b.top_k if k is None else k)))


def cbatch_out(b: CodingBatch, db: Session) -> dict:
    n, reviewed, uplift = db.execute(
        select(func.count(), func.count(CodingClaim.review_status), func.coalesce(func.sum(CodingClaim.uplift), 0))
        .where(CodingClaim.batch_id == b.id, _queued(b))
    ).one()
    hospitals = b.hospitals or []
    planted = db.scalar(select(func.count()).select_from(CodingClaim).where(
        CodingClaim.batch_id == b.id, CodingClaim.label.is_not(None))) if b.labelled else 0
    return {
        "planted": planted,
        "id": b.id, "name": b.name, "filename": b.filename, "uploadedBy": b.uploaded_by, "uploadedAt": iso(b.uploaded_at),
        "rowsTotal": b.rows_total, "rowsRead": b.rows_read, "bad": b.skipped_bad, "truncated": b.truncated,
        "groups": b.groups, "compared": b.compared, "hospitalsN": b.hospitals_n,
        "topK": b.top_k, "maxTopK": coding.max_top_k(b.compared), "baseScore": b.base_score,
        "labelled": b.labelled, "own": b.owner is not None, "queued": n, "reviewed": reviewed, "uplift": int(uplift or 0),
        "reviewHospitals": sum(1 for h in hospitals if h.get("verdict") == "tinjau"),
        "smallBase": b.hospitals_n < coding.MIN_HOSPITALS_FOR_PEERS,
    }


def cclaim_out(c: CodingClaim, full: bool = False) -> dict:
    out = {
        "id": c.id, "sep": c.sep, "rs": c.kode_rs, "kelasRs": c.kelas_rs, "admTxt": c.adm_date, "disTxt": c.dis_date,
        "cbg": c.inacbg, "grp": c.grp, "sev": c.sev, "nSec": c.n_sec, "los": c.los, "icu": c.icu, "vent": c.vent,
        "died": c.died, "tarif": c.tarif, "uplift": c.uplift, "tested": c.tested, "supported": c.supported,
        "implied": c.implied, "typLos": c.typ_los, "typDx": c.typ_dx, "baseLos": c.base_los, "careDx": c.care_dx,
        "pPeer": c.p_peer, "hz": c.hz, "hr": c.hr, "rule": c.rule, "live": c.live, "f": [c.f1, c.f2, c.f3, c.f4],
        "score": c.score, "rank": c.rank, "phi": c.phi, "label": c.label, "review": review_out(c),
    }
    if full:
        out.update({"cbgDesc": c.inacbg_desc, "diag": c.diag, "proc": c.proc})
    return out


@app.get("/api/coding/batches")
def list_cbatches(user: User = Depends(current_user), db: Session = Depends(get_db)):
    owner = visible_owner(user)
    q = select(CodingBatch).where(CodingBatch.owner.is_(None) if owner is None else CodingBatch.owner == owner)
    return [cbatch_out(b, db) for b in db.scalars(q.order_by(CodingBatch.id.desc())).all()]


@app.post("/api/coding/batches", dependencies=[Depends(csrf)])
def upload_cbatch(
    file: UploadFile = File(...), name: str = Form(""),
    user: User = Depends(current_user), db: Session = Depends(get_db),
):
    owner = visible_owner(user)
    if owner is not None:
        n = db.scalar(select(func.count()).select_from(CodingBatch).where(CodingBatch.owner == owner))
        if n >= settings.guest_max_batches:
            raise HTTPException(400, "Mode juri dibatasi {} batch. Hapus satu batch dulu.".format(settings.guest_max_batches))
    raw = file.file.read(settings.max_upload_bytes + 1)
    if len(raw) > settings.max_upload_bytes:
        raise HTTPException(413, "Berkas lebih besar dari {} MB, batas untuk prototype ini.".format(
            settings.max_upload_bytes // (1024 * 1024)))
    filename = os.path.basename(file.filename or "upload.txt")
    label = name.strip() or re.sub(r"\.[A-Za-z0-9]{1,5}$", "", filename) or "Batch tanpa nama"
    try:
        batch = services.ingest_coding(db, eklaim.decode(raw), name=label, filename=filename, user=user, owner=owner)
    except eklaim.EklaimError as exc:
        raise HTTPException(400, str(exc)) from None
    return cbatch_out(batch, db)


@app.get("/api/coding/batches/{batch_id}")
def cbatch_detail(batch_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    batch = get_cbatch(batch_id, db, user)
    # enough claims for the capacity slider to reach its maximum without another request
    claims = db.scalars(
        select(CodingClaim).where(CodingClaim.batch_id == batch_id, _queued(batch, coding.max_top_k(batch.compared)))
        .order_by(CodingClaim.rank)
    ).all()
    return {"batch": cbatch_out(batch, db), "claims": [cclaim_out(c) for c in claims], "hospitals": batch.hospitals or []}


class CodingSettingsIn(BaseModel):
    topK: int = Field(ge=1, le=200)


@app.patch("/api/coding/batches/{batch_id}", dependencies=[Depends(csrf)])
def cbatch_settings(batch_id: int, body: CodingSettingsIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    batch = get_cbatch(batch_id, db, user)
    if not can_manage(user):
        raise HTTPException(403, "Hanya supervisor yang dapat melakukan ini.")
    top_k = min(body.topK, coding.max_top_k(batch.compared))
    before = batch.top_k
    batch.top_k = top_k
    services.audit(db, user, "coding_settings", batch_id=batch.id, detail={"from": {"topK": before}, "to": {"topK": top_k}, "modul": "koding"})
    db.commit()
    return cbatch_out(batch, db)


@app.delete("/api/coding/batches/{batch_id}", dependencies=[Depends(csrf)])
def cbatch_delete(batch_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    batch = get_cbatch(batch_id, db, user)
    if not can_manage(user):
        raise HTTPException(403, "Hanya supervisor yang dapat melakukan ini.")
    services.audit(db, user, "coding_delete", batch_id=batch.id, detail={"name": batch.name, "modul": "koding"})
    services.delete_coding_batch(db, batch)
    db.commit()
    return {"ok": True}


@app.get("/api/coding/batches/{batch_id}/export.csv")
def cbatch_export(
    batch_id: int, k: Optional[int] = Query(None, ge=1, le=200),
    user: User = Depends(current_user), db: Session = Depends(get_db),
):
    batch = get_cbatch(batch_id, db, user)
    k = min(k, coding.max_top_k(batch.compared)) if k is not None else batch.top_k
    claims = db.scalars(
        select(CodingClaim).where(CodingClaim.batch_id == batch_id, _queued(batch, k)).order_by(CodingClaim.rank)
    ).all()
    labels = [f["label"] for f in coding.FEATURES]
    buf = io.StringIO()
    w = csv.writer(buf, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
    w.writerow(["SEP", "KODE_RS", "KELAS_RS", "INACBG", "CODED_LEVEL", "LEVEL_SHOWN_BY_CARE", "SECONDARY_DX",
                "SECONDARY_DX_USUAL_FOR_CARE", "LOS", "LOS_USUAL_FOR_CODED_LEVEL", "ICU", "VENTILATOR", "DIED",
                "TARIF_INACBG", "ESTIMATED_TARIFF_DIFFERENCE", "PEER_SHARE_LEVEL_II_III", "HOSPITAL_Z",
                "RULE", "ANOMALY_SCORE", "RANK", "MAIN_DRIVER", "PHI_HOSPITAL", "PHI_STAY", "PHI_DIAGNOSES",
                "PHI_TARIFF", "REVIEW_STATUS", "REVIEW_NOTE", "RS_RESPONSE", "REVIEWED_BY", "REVIEWED_AT"])
    for c in claims:
        phi = c.phi or [None] * 4
        driver = labels[max(range(4), key=lambda i: phi[i])] if c.phi else ""
        w.writerow([_csv_cell(v) for v in [
            c.sep, c.kode_rs, c.kelas_rs, c.inacbg, "I" * c.sev, "I" * c.implied if c.implied else "", c.n_sec,
            "%g" % c.care_dx, c.los, "%g" % c.typ_los, int(c.icu), int(c.vent), int(c.died), c.tarif, c.uplift,
            "" if c.p_peer is None else "%.3f" % c.p_peer, "%.2f" % c.hz, "flagged" if c.rule else "pass",
            "%.4f" % c.score, c.rank, driver, *["" if x is None else "%.4f" % x for x in phi],
            c.review_status or "", c.review_note, c.rs_response or "", c.reviewed_by or "", iso(c.reviewed_at) or "",
        ]])
    services.audit(db, user, "coding_export", batch_id=batch.id, detail={"rows": len(claims), "topK": k, "modul": "koding"})
    db.commit()
    return Response(
        "\ufeff" + buf.getvalue(), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="veriklaim-koding-{}.csv"'.format(batch.id)},
    )


def get_cclaim(claim_id: int, db: Session, user: User) -> CodingClaim:
    claim = db.get(CodingClaim, claim_id)
    batch = db.get(CodingBatch, claim.batch_id) if claim is not None else None
    if claim is None or batch is None or batch.owner != visible_owner(user):
        raise HTTPException(404, "Klaim tidak ditemukan. Batch-nya mungkin sudah dihapus.")
    return claim


@app.get("/api/coding/claims/{claim_id}")
def cclaim_detail(claim_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    claim = get_cclaim(claim_id, db, user)
    services.ensure_coding_phi(db, claim)
    return cclaim_out(claim, full=True)


@app.put("/api/coding/claims/{claim_id}/review", dependencies=[Depends(csrf)])
def cclaim_review(claim_id: int, body: ReviewIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    claim = get_cclaim(claim_id, db, user)
    before = claim.review_status
    note, response = body.note.strip(), body.rsResponse.strip()
    if body.status is None:
        claim.review_status, claim.review_note, claim.reviewed_by, claim.reviewed_at = None, "", None, None
        claim.rs_response = ""
    else:
        claim.review_status, claim.review_note = body.status, note
        claim.reviewed_by, claim.reviewed_at = user.username, utcnow()
        claim.rs_response = response
    detail = {"from": before, "to": body.status, "note": note[:200], "modul": "koding"}
    if response:
        detail["rsResponse"] = response[:200]
    services.audit(db, user, "coding_review_set" if body.status else "coding_review_reopened", batch_id=claim.batch_id,
                   sep=claim.sep, detail=detail)
    db.commit()
    return cclaim_out(claim, full=True)


# ---------- reference prices ----------

def ref_out(r: ReferencePrice) -> dict:
    return {"code": r.code, "kind": r.kind, "type": eklaim.KIND_NAMES.get(r.kind, r.kind),
            "description": r.description, "price": r.price, "source": r.source,
            "updatedBy": r.updated_by, "updatedAt": iso(r.updated_at)}


@app.get("/api/reference-prices")
def list_refs(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return [ref_out(r) for r in db.scalars(select(ReferencePrice).order_by(ReferencePrice.code))]


@lru_cache(maxsize=1)
def _special_cmg() -> dict:
    return json.loads((settings.data_dir / "special_cmg.json").read_text(encoding="utf-8"))


@app.get("/api/special-cmg")
def special_cmg(user: User = Depends(current_user), db: Session = Depends(get_db)):
    """The official Special CMG codes (Special Drug and Special Prosthesis) and whether each has a reference price."""
    try:
        catalog = _special_cmg()
    except (OSError, ValueError):
        raise HTTPException(404, "Daftar kode Special CMG tidak tersedia pada instalasi ini.")
    prices = {r.code: r.price for r in db.scalars(select(ReferencePrice))}
    codes = [{**c, "type": eklaim.KIND_NAMES.get(c["kind"], c["kind"]), "priced": c["code"] in prices,
              "price": prices.get(c["code"])} for c in catalog["codes"]]
    return {"source": catalog["source"], "codes": codes,
            "total": len(codes), "priced": sum(1 for c in codes if c["priced"])}


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
def audit_log(limit: int = Query(200, ge=1, le=1000), user: User = Depends(current_user), db: Session = Depends(get_db)):
    q = select(AuditLog)
    if user.role == services.GUEST_ROLE:
        q = q.where(AuditLog.username == user.username)  # a guest sees their own trail
    elif user.role != "supervisor":
        raise HTTPException(403, "Hanya supervisor yang dapat melakukan ini.")
    rows = db.scalars(q.order_by(AuditLog.id.desc()).limit(limit)).all()
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

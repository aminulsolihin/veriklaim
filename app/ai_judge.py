"""Second opinion from Claude on one top-up claim.

The model sees what the verifier sees in the detail panel, minus every identifier: no SEP number,
no card token. It also sees a handful of claims the team has already decided, preferring the same
top-up item, so the opinion follows this team's past decisions (in-context learning; nothing is
trained or uploaded for training).

The opinion is advice only. It is never saved as a review decision; the verifier still decides.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from sqlalchemy import case, select
from sqlalchemy.orm import Session

from . import scoring
from .config import settings
from .db import Batch, Claim

log = logging.getLogger("veriklaim.ai")

N_EXAMPLES = 8
FALLBACK_BETA = "server-side-fallback-2026-07-01"

SYSTEM = """You are a second-opinion reviewer for BPJS Kesehatan claim verifiers in Indonesia.
Each claim is an INA-CBG inpatient claim with a Special CMG top-up (a special prosthesis "SR" or a
special drug "SD"). A screening tool has already compared the hospital bill (TARIF_RS) with the
reference price of the top-up item and run an anomaly model over four features. You receive those
numbers, the model's per-feature Shapley contributions, and examples of claims this team has
already decided.

Recommend one of:
- "confirmed": the top-up is not supported by the claim (for example the hospital bill is far
  below what the item costs, or the same top-up repeats for the same card within 90 days).
- "cleared": the claim looks consistent; the flag has a plausible benign cause.
- "docs_requested": the claim cannot be decided from these fields; name the documents to ask for
  (for example the implant invoice, the operation report, the drug administration record).

Rules:
- Base the opinion only on the fields given. Do not invent clinical details.
- A hospital bill below the reference price is the strongest signal, but referral hospitals,
  catalogue purchases below list price and staged operations are legitimate causes.
- When the examples conflict with your own reading, follow the team's past decisions and say so.
- Prefer "docs_requested" over a confident answer when the evidence is thin.
- Write the summary and reasons in Bahasa Indonesia, short and plain, for a verifier."""

SCHEMA = {
    "type": "object",
    "properties": {
        "recommendation": {"type": "string", "enum": ["confirmed", "cleared", "docs_requested"]},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "summary": {"type": "string"},
        "reasons": {"type": "array", "items": {"type": "string"}},
        "documents": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["recommendation", "confidence", "summary", "reasons", "documents"],
    "additionalProperties": False,
}


class AiJudgeError(RuntimeError):
    pass


_client = None


def _get_client():
    global _client
    if _client is None:
        import anthropic  # imported lazily so the app runs without the package when the feature is off
        _client = anthropic.Anthropic(timeout=90.0, max_retries=2)
    return _client


def describe(c: Claim) -> dict:
    """The fields the model may see. Keep identifiers (sep, card_token) out of here."""
    phi = c.phi or [None] * scoring.N_FEATURES
    return {
        "hospital": {"code": c.kode_rs, "class": c.kelas_rs, "tariff_code": c.kode_tarif},
        "stay": {"admission": c.adm_date, "discharge": c.dis_date, "los_days": c.los,
                 "median_los_same_cbg": c.los_median},
        "ina_cbg": {"code": c.inacbg, "description": c.inacbg_desc,
                    "diagnoses": c.diag, "procedures": c.proc},
        "top_up": {"kind": c.cmg_kind, "code": c.cmg_final or c.cmg_code, "item": c.cmg_desc,
                   "amount_rp": c.topup},
        "money_rp": {"tarif_rs": c.tarif_rs, "tarif_inacbg": c.tarif_inacbg, "total_tarif": c.total_tarif,
                     "reference_price_of_item": c.ref_price},
        "features": {
            "tarif_rs_over_reference": round(c.cover, 3),
            "los_over_median": round(c.los_ratio, 3),
            "same_topup_same_card_90d": c.repeat_n,
            "hospital_topup_volume_over_peers": round(c.vol_ratio, 3),
        },
        "anomaly_model": {
            "score": round(c.score, 4), "rank": c.rank,
            "shapley": {f["key"]: (round(v, 4) if v is not None else None) for f, v in zip(scoring.FEATURES, phi)},
        },
    }


def examples(db: Session, claim: Claim, owner: Optional[str], n: int = N_EXAMPLES) -> list[dict]:
    """Claims this team already decided, same top-up item first, then the most recent."""
    same_item = case((Claim.cmg_code == claim.cmg_code, 0), else_=1)
    rows = db.scalars(
        select(Claim).join(Batch, Batch.id == Claim.batch_id)
        .where(Batch.owner.is_(None) if owner is None else Batch.owner == owner,
               Claim.review_status.in_(["confirmed", "cleared"]), Claim.id != claim.id)
        .order_by(same_item, Claim.reviewed_at.desc()).limit(n)
    ).all()
    return [{"claim": describe(r), "team_decision": r.review_status, "verifier_note": (r.review_note or "")[:300]}
            for r in rows]


def opinion(db: Session, claim: Claim, owner: Optional[str]) -> dict:
    if not settings.ai_judge:
        raise AiJudgeError("Pendapat AI belum diaktifkan di server ini.")
    import anthropic

    shots = examples(db, claim, owner)
    content = (
        "Past decisions by this team:\n" + json.dumps(shots, ensure_ascii=False)
        + "\n\nClaim to review:\n" + json.dumps(describe(claim), ensure_ascii=False)
    ) if shots else "No past decisions yet.\n\nClaim to review:\n" + json.dumps(describe(claim), ensure_ascii=False)
    try:
        resp = _get_client().beta.messages.create(
            model=settings.ai_model,
            max_tokens=16000,
            betas=[FALLBACK_BETA],
            fallbacks="default",
            system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": content}],
            output_config={"effort": "high", "format": {"type": "json_schema", "schema": SCHEMA}},
        )
    except anthropic.RateLimitError:
        raise AiJudgeError("Layanan AI sedang sibuk. Coba lagi sebentar lagi.")
    except (anthropic.APIStatusError, anthropic.APIConnectionError) as e:
        log.warning("Claude request failed: %s", e)
        raise AiJudgeError("Layanan AI tidak dapat dihubungi. Coba lagi nanti.")

    if resp.stop_reason == "refusal":
        raise AiJudgeError("AI menolak menilai klaim ini. Lanjutkan pemeriksaan manual.")
    text = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        out = json.loads(text)
    except ValueError:
        log.warning("Unparseable AI output (stop_reason=%s, request=%s)", resp.stop_reason, resp._request_id)
        raise AiJudgeError("Jawaban AI tidak lengkap. Coba lagi.")
    out.update({"model": resp.model, "examplesUsed": len(shots)})
    return out

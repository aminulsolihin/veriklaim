"""Upcoding indicators for INA-CBG claims.

An INA-CBG code ends in a severity level (I, II or III) that the grouper derives from the
diagnoses and procedures entered, and a claim coded at a higher level is paid more. This module
asks one narrow question of every claim coded II or III: is that level supported by the rest of
the claim, and by what other hospitals do for the same case group?

  Stage A  case groups      claims are compared only within the same base group (the first three
                            parts of the code, for example I-4-10) and only when the file holds
                            enough claims at two or more severity levels.
  Stage B  rule             a claim coded II or III that carries at least two more secondary
                            diagnoses than the care it shows normally carries, with no ICU or
                            ventilator use and a stay no longer than a claim at the lowest level.
  Stage C  anomaly layer    scikit-learn Isolation Forest over four one-sided features, each of
                            which grows as a claim looks less supported (same model class and
                            exact Shapley values as the top-up screen, see scoring.py).
  Hospital pattern          observed against expected claims coded II or III, adjusted for the
                            mix of case groups, and how many of that hospital's claims are
                            supported by the care given. A referral hospital with long, intensive
                            stays can code high severity without being flagged.

This is not a medical necessity review. Whether care was needed is a clinical judgement made from
the medical record by a medical verifier. These indicators rank claims for that review.
"""
from __future__ import annotations

import math
import re
import statistics
from collections import defaultdict
from typing import Iterable, Optional

import numpy as np

from . import scoring
from .eklaim import EklaimError, _clip, to_number

REQUIRED = ["KODE_RS", "SEP", "INACBG", "LOS", "DIAGLIST"]
MIN_ROWS = 60           # a file smaller than this has too little to compare against
MIN_GROUP = 12          # claims in a case group before it is compared
MIN_LEVEL = 3           # claims at one severity level before that level's typical values are used
MIN_PEER = 8            # claims at other hospitals needed to estimate the peer share
MIN_HOSP = 15           # compared claims at one hospital before it gets a z-score
MIN_HOSPITALS_FOR_PEERS = 5   # fewer hospitals than this and "other hospitals" is too thin a baseline
MIN_HIGH = 8            # a hospital's compared II/III claims before its support rate is used
Z_FLAG = 2.33           # one-sided p of about 0.01
EXCESS_FLAG = 3.0       # at least this many more II/III claims than expected
SUPPORT_GAP = 0.10      # a hospital is "didukung" if its support rate is within this of the median hospital
RULE_EXCESS = 2.0       # secondary diagnoses above what the care shown normally carries

FEATURES = [
    {"key": "rs", "label": "RS: tingkat berat melebihi sejawat"},
    {"key": "los", "label": "Lama rawat lebih pendek dari lazim"},
    {"key": "dx", "label": "Diagnosis sekunder melebihi perawatan"},
    {"key": "uplift", "label": "Kenaikan tarif karena tingkat"},
]

_CBG = re.compile(r"^([A-Z]-\d-\d{2})-(0|I{1,3})$")
_LEVEL = {"0": 0, "I": 1, "II": 2, "III": 3}
_SPLIT = re.compile(r"[#;,|\s]+")


def split_codes(text: str) -> list[str]:
    return [c for c in _SPLIT.split((text or "").strip()) if c and c not in ("-", "None")]


def parse_coding(text: str) -> dict:
    """Read every claim of an E-Klaim TXT export. Returns {"rows", "total", "bad", "dup"}; `bad` includes the duplicate-SEP rows.

    Only what the check needs is read. Patient name, MRN, birth date and card number are not."""
    lines = [ln for ln in (text or "").replace("\r", "").split("\n") if ln.strip()]
    if len(lines) < 2:
        raise EklaimError("Berkas tidak memuat baris klaim. TXT E-Klaim berisi satu baris judul kolom, lalu satu baris per SEP.")
    head = [h.strip().upper() for h in lines[0].split("\t")]
    if len(head) < 10:
        raise EklaimError(
            "Baris pertama bukan judul kolom E-Klaim yang dipisah tab (tab-separated). TXT terenkripsi "
            "belum bisa dibaca; gunakan ekspor TXT biasa."
        )
    ix = {h: i for i, h in enumerate(head)}
    missing = [k for k in REQUIRED if k not in ix]
    if missing:
        raise EklaimError("Kolom tidak ditemukan: {}.".format(", ".join(missing)))

    def g(cells: list[str], key: str) -> str:
        i = ix.get(key)
        return cells[i].strip() if i is not None and i < len(cells) else ""

    rows: list[dict] = []
    bad = dup = 0
    seen: set[str] = set()
    for line in lines[1:]:
        c = line.split("\t")
        sep, cbg = g(c, "SEP"), g(c, "INACBG").upper()
        m = _CBG.match(cbg)
        if not sep or not m:
            bad += 1
            continue
        if sep in seen:  # a repeated SEP is one claim exported twice; keep the first
            dup += 1
            continue
        seen.add(sep)
        codes = split_codes(g(c, "DIAGLIST"))
        status = g(c, "DISCHARGE_STATUS")
        rows.append({
            "sep": _clip(sep, 40),
            "kode_rs": _clip(g(c, "KODE_RS"), 20) or "?",
            "kelas_rs": _clip(g(c, "KELAS_RS"), 8),
            "adm_date": _clip(g(c, "ADMISSION_DATE"), 20),
            "dis_date": _clip(g(c, "DISCHARGE_DATE"), 20),
            "inacbg": cbg,
            "inacbg_desc": _clip(g(c, "DESKRIPSI_INACBG"), 200),
            "grp": m.group(1),
            "sev": _LEVEL[m.group(2)],
            "diag": _clip("#".join(codes), 200),
            "proc": _clip("#".join(split_codes(g(c, "PROCLIST"))), 200),
            "n_sec": max(0, len(codes) - 1),
            "los": min(100_000, max(0, int(to_number(g(c, "LOS"))))),
            "icu": g(c, "ICU_INDIKATOR") in ("1", "Y", "y") or to_number(g(c, "ICU_LOS")) > 0,
            "vent": to_number(g(c, "VENT_HOUR")) > 0,
            "died": status == "4",
            "tarif": min(10 ** 12, int(to_number(g(c, "TARIF_INACBG")))),
        })
    return {"rows": rows, "total": len(lines) - 1, "bad": bad + dup, "dup": dup}


def _median(values: list[float]) -> float:
    return float(statistics.median(values))


def _z_p(z: float) -> float:
    """One-sided p for a standard normal z."""
    return 0.5 * math.erfc(z / math.sqrt(2))


def analyse(rows: list[dict]) -> dict:
    """Add the indicators to each row in place and return {"matrix", "groups", "hospitals", "high"}.

    Every feature is one-sided: it is zero unless the claim looks less supported, so the anomaly
    layer isolates claims that look unsupported and not claims that are merely long or complex."""
    by_group: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_group[r["grp"]].append(r)

    groups: dict[str, dict] = {}
    for g, rs in by_group.items():
        if len(rs) < MIN_GROUP:
            continue
        at = {s: [r for r in rs if r["sev"] == s] for s in (1, 2, 3)}
        levels = [s for s in (1, 2, 3) if len(at[s]) >= MIN_LEVEL]
        if len(levels) < 2:
            continue
        base = levels[0]
        tarif_class: dict[str, float] = {}
        for k in {r["kelas_rs"] for r in at[base]}:
            vals = [r["tarif"] for r in at[base] if r["kelas_rs"] == k and r["tarif"] > 0]
            if len(vals) >= MIN_LEVEL:
                tarif_class[k] = _median(vals)
        base_tarif = [r["tarif"] for r in at[base] if r["tarif"] > 0]
        groups[g] = {
            "n": len(rs), "k_high": sum(1 for r in rs if r["sev"] >= 2), "levels": levels, "base": base,
            "los": {s: _median([max(1, r["los"]) for r in at[s]]) for s in levels},
            "dx": {s: _median([r["n_sec"] for r in at[s]]) for s in levels},
            "tarif_class": tarif_class,
            "tarif_any": _median(base_tarif) if len(base_tarif) >= MIN_LEVEL else 0.0,
        }

    n_hg: dict[tuple, int] = defaultdict(int)
    k_hg: dict[tuple, int] = defaultdict(int)
    for r in rows:
        if r["grp"] in groups:
            n_hg[(r["kode_rs"], r["grp"])] += 1
            k_hg[(r["kode_rs"], r["grp"])] += 1 if r["sev"] >= 2 else 0

    # Per claim: the level of care the claim actually shows, and what that level normally carries
    # in diagnoses.
    for r in rows:
        r["tested"] = r["grp"] in groups
        r["p_peer"], r["supported"], r["implied"] = None, False, 0
        r["typ_los"] = r["typ_dx"] = r["base_los"] = r["care_dx"] = 0.0
        r["uplift"] = 0
        if not r["tested"]:
            continue
        gi = groups[r["grp"]]
        r["base_los"] = gi["los"][gi["base"]]
        r["typ_los"] = gi["los"].get(r["sev"], 0.0)
        r["typ_dx"] = gi["dx"].get(r["sev"], 0.0)
        if r["icu"] or r["vent"]:
            r["implied"] = gi["levels"][-1]
        else:
            r["implied"] = min(gi["levels"], key=lambda s: abs(math.log2(max(1, r["los"]) / gi["los"][s])))
        r["care_dx"] = gi["dx"][r["implied"]]
        # a claim is supported when ICU, ventilator or death explains the level, or the stay is
        # at least as long as a claim of that level normally has
        r["supported"] = bool(r["icu"] or r["vent"] or r["died"] or r["implied"] >= r["sev"])
        base_tarif = gi["tarif_class"].get(r["kelas_rs"]) or gi["tarif_any"]
        if r["sev"] > gi["base"] and base_tarif > 0 and r["tarif"] > 0:
            r["uplift"] = max(0, int(r["tarif"] - base_tarif))

    def set_peer_share(skip: set) -> None:
        """Share of claims coded II/III at the other hospitals in the same case group. Hospitals
        in `skip` do not count as peers, so a hospital that stands out does not set the norm."""
        gn: dict[str, int] = defaultdict(int)
        gk: dict[str, int] = defaultdict(int)
        for (rs, g), n in n_hg.items():
            if rs not in skip:
                gn[g] += n
                gk[g] += k_hg[(rs, g)]
        for r in rows:
            r["p_peer"] = None
            if not r["tested"]:
                continue
            own = 0 if r["kode_rs"] in skip else 1
            peers = gn[r["grp"]] - own * n_hg[(r["kode_rs"], r["grp"])]
            if peers >= MIN_PEER:
                p = (gk[r["grp"]] - own * k_hg[(r["kode_rs"], r["grp"])]) / peers
                r["p_peer"] = min(0.99, max(0.01, p))

    def tabulate() -> list[dict]:
        return hospital_table(
            [(r["kode_rs"], r["kelas_rs"], r["sev"], r["p_peer"], r["supported"], False) for r in rows if r["tested"]]
        )

    set_peer_share(set())
    first = tabulate()
    stand_out = {h["rs"] for h in first if h["z"] >= Z_FLAG and h["high"] - h["expected"] >= EXCESS_FLAG}
    if stand_out:
        set_peer_share(stand_out)
        table = tabulate()
    else:
        table = first
    stat = {h["rs"]: h for h in table}

    X = np.zeros((len(rows), scoring.N_FEATURES))
    for i, r in enumerate(rows):
        h = stat.get(r["kode_rs"])
        r["hz"] = float(h["z"]) if h else 0.0
        r["hr"] = float(h["support"]) if h and h["support"] is not None else None
        f1 = f2 = f3 = f4 = 0.0
        r["rule"] = False
        scored = r["tested"] and r["sev"] >= 2 and r["sev"] in groups[r["grp"]]["los"] and not r["died"]
        if scored:
            gi = groups[r["grp"]]
            if r["hr"] is not None and r["p_peer"] is not None:
                f1 = min(6.0, max(0.0, r["hz"])) * (1.0 - r["hr"])
            f2 = min(3.0, max(0.0, math.log2(r["typ_los"] / max(1, r["los"]))))
            f3 = min(6.0, max(0.0, r["n_sec"] - r["care_dx"]))
            base_tarif = gi["tarif_class"].get(r["kelas_rs"]) or gi["tarif_any"]
            if r["sev"] > gi["base"] and base_tarif > 0 and r["tarif"] > 0:
                f4 = min(2.0, max(0.0, math.log2(r["tarif"] / base_tarif)))
            r["rule"] = bool(
                r["n_sec"] - r["care_dx"] >= RULE_EXCESS and not r["icu"] and not r["vent"] and r["los"] <= r["base_los"]
            )
        r["f"] = [f1, f2, f3, f4]
        X[i] = r["f"]
    n_high = sum(1 for r in rows if r["tested"] and r["sev"] >= 2)
    return {"matrix": X, "groups": len(groups), "hospitals": table, "high": n_high}


def hospital_table(items: Iterable[tuple]) -> list[dict]:
    """One row per hospital from (kode_rs, kelas_rs, sev, p_peer, supported, queued) tuples of
    the claims that were compared. Sorted with the strongest pattern first."""
    acc: dict[str, dict] = defaultdict(lambda: {"kelas": "", "n": 0, "o": 0, "e": 0.0, "v": 0.0, "high": 0,
                                                "supported": 0, "queued": 0})
    for rs, kelas, sev, p, supported, queued in items:
        if p is None:
            continue
        h = acc[rs]
        h["kelas"] = h["kelas"] or kelas
        h["n"] += 1
        h["o"] += 1 if sev >= 2 else 0
        h["e"] += p
        h["v"] += p * (1 - p)
        if sev >= 2:
            h["high"] += 1
            h["supported"] += 1 if supported else 0
        h["queued"] += 1 if queued else 0
    rates = [h["supported"] / h["high"] for h in acc.values() if h["high"] >= MIN_HIGH]
    ref = statistics.median(rates) if rates else None
    out = []
    for rs, h in acc.items():
        z = 0.0
        if h["n"] >= MIN_HOSP and h["v"] > 0:
            diff = h["o"] - h["e"]
            z = (diff - 0.5 if diff > 0 else diff) / math.sqrt(h["v"])
        support = h["supported"] / h["high"] if h["high"] >= MIN_HIGH else None
        flagged = h["n"] >= MIN_HOSP and z >= Z_FLAG and h["o"] - h["e"] >= EXCESS_FLAG
        if h["n"] < MIN_HOSP:
            verdict = "sedikit"
        elif not flagged:
            verdict = "wajar"
        elif support is not None and ref is not None and support >= ref - SUPPORT_GAP:
            verdict = "didukung"
        else:
            verdict = "tinjau"
        out.append({
            "rs": rs, "kelas": h["kelas"], "n": h["n"], "high": h["o"], "expected": h["e"],
            "ratio": h["o"] / h["e"] if h["e"] > 0 else None, "z": z, "p": _z_p(z) if h["n"] >= MIN_HOSP else None,
            "support": support, "support_ref": ref, "verdict": verdict, "queued": h["queued"],
        })
    out.sort(key=lambda d: (-d["z"], d["rs"]))
    return out


def screen(rows: list[dict]) -> tuple[scoring.Model, dict]:
    """Analyse, fit, score and rank `rows` in place; explain the most unusual ones.

    Rank 1 is the most unusual claim. Claims whose four features are all zero (nothing about them
    looks unsupported) rank below every other claim, whatever their score."""
    info = analyse(rows)
    model = scoring.Model(info["matrix"])
    order = sorted(range(len(rows)), key=lambda i: (not any(rows[i]["f"]), -float(model.scores[i]), i))
    for rank, i in enumerate(order, start=1):
        r = rows[i]
        r["pos"] = i
        r["live"] = any(r["f"])
        r["score"] = float(model.scores[i])
        r["rank"] = rank
        r["phi"] = None
    eager = [i for i in order[: scoring.EAGER_EXPLAIN] if rows[i]["live"]]
    for i, phi in zip(eager, model.shapley(eager)):
        rows[i]["phi"] = [float(x) for x in phi]
    return model, info


def default_top_k(n_high: int) -> int:
    """Claims coded II or III that were compared: review about one in eight by default."""
    return min(max_top_k(n_high), max(4, round(n_high * 0.12)))


def max_top_k(n_high: int) -> int:
    return max(4, min(200, n_high * 3 // 10))


def queue_positions(rows: list[dict], top_k: int) -> set[int]:
    """Positions of the claims to review: every rule flag, plus the `top_k` highest-ranked claims
    that have at least one non-zero feature (a claim that looks fully supported is not queued
    just because the list is not full)."""
    return {r["pos"] for r in rows if r["rule"] or (r["live"] and r["rank"] <= top_k)}

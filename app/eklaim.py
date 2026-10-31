"""Reader for the E-Klaim INA-CBG TXT export.

Layout: one header line and one line per SEP, tab-separated, as listed for the
pull_claim method in Petunjuk Teknis E-Klaim 5.1.1. Columns are matched by header
name, so extra or reordered columns are fine.

NAMA_PASIEN, MRN and BIRTH_DATE are never read. NOKARTU is turned into a one-way
token before the row leaves this module.
"""
from __future__ import annotations

import math
import re
from datetime import date
from typing import Callable, Optional

from . import baseline

NEEDED = ["KODE_RS", "KODE_TARIF", "ADMISSION_DATE", "INACBG", "TARIF_RS", "LOS", "SEP", "NOKARTU"]
KIND_NAMES = {"SR": "Special Prosthesis", "SD": "Special Drug"}

_DMY = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})")
_YMD = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})")
_CODE = re.compile(r"^([A-Z]{2})-?(\d{2})")
_NOT_NUM = re.compile(r"[^0-9.,\-]")
MAX_NUMBER = 10 ** 13   # larger values are clamped to this, so they can neither overflow a column nor raise


class EklaimError(ValueError):
    """The file cannot be screened. The message is safe to show to the user."""


def decode(raw: bytes) -> str:
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def parse_day(text: str) -> Optional[int]:
    text = (text or "").strip()
    try:
        m = _DMY.match(text)
        if m:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1))).toordinal()
        m = _YMD.match(text)
        if m:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).toordinal()
    except ValueError:
        return None
    return None


def norm_code(value: str) -> Optional[str]:
    """'RR04', 'RR-04-III' -> 'RR04'. 'None', '-' and '' -> None."""
    v = (value or "").strip().upper()
    if not v or v in ("NONE", "-"):
        return None
    m = _CODE.match(v)
    return m.group(1) + m.group(2) if m else None


def to_number(value: str) -> float:
    """Read a number written either way: 25000000.50, 25,000,000.50 or 25.000.000,50.

    The last '.' or ',' is the decimal mark when both appear. A lone separator is a decimal mark
    when 1-2 digits follow it and a thousands mark when exactly 3 do ('25.000' is 25000). Unreadable
    text gives 0.0; infinite or larger-than-MAX_NUMBER values are clamped to MAX_NUMBER."""
    s = _NOT_NUM.sub("", value or "")
    if "-" in s[1:]:
        return 0.0
    neg = s.startswith("-")
    s = s.replace("-", "")
    if "." in s and "," in s:
        dec = "." if s.rfind(".") > s.rfind(",") else ","
        s = s.replace("," if dec == "." else ".", "").replace(dec, ".")
    elif "." in s or "," in s:
        sep = "." if "." in s else ","
        tail = s.rsplit(sep, 1)[1]
        if s.count(sep) > 1 or len(tail) == 3:
            s = s.replace(sep, "")
        else:
            s = s.replace(sep, ".")
    try:
        v = float(s)
    except ValueError:
        return 0.0
    v = MAX_NUMBER if not math.isfinite(v) else min(v, MAX_NUMBER)
    return -v if neg else v


def _clip(value: str, n: int) -> str:
    return (value or "").strip()[:n]


def parse_eklaim(text: str, refs: dict[str, dict], tokenise: Callable[[str], str]) -> dict:
    """Return {"rows": [...], "episodes": [...], "total", "no_topup", "no_ref", "bad", "dup"}. `bad` includes
    the `dup` rows (a SEP seen earlier in the file; the first one is kept).

    `refs` maps a Special CMG code to {"kind", "description", "price"}. Only claims
    whose Special Prosthesis or Special Drug code has a reference price are kept.

    `episodes` holds a light record (card token, stay, primary diagnosis, hospital) of every claim in the file,
    with or without a top-up, whose card also has a kept claim; episodes.py checks them for overlapping stays.
    """
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
    missing = [k for k in NEEDED if k not in ix]
    if "IN_SR" not in ix and "SR" not in ix:
        missing.append("IN_SR atau SR")
    if "IN_SD" not in ix and "SD" not in ix:
        missing.append("IN_SD atau SD")
    if missing:
        raise EklaimError("Kolom tidak ditemukan: {}.".format(", ".join(missing)))

    def g(cells: list[str], key: str) -> str:
        i = ix.get(key)
        return cells[i].strip() if i is not None and i < len(cells) else ""

    rows: list[dict] = []
    no_topup = no_ref = bad = dup = 0
    seen: set[str] = set()
    cards: set[str] = set()   # raw card numbers of kept claims; compared here only, never stored
    for line in lines[1:]:
        c = line.split("\t")
        sr = norm_code(g(c, "IN_SR")) or norm_code(g(c, "SR"))
        sd = norm_code(g(c, "IN_SD")) or norm_code(g(c, "SD"))
        if not sr and not sd:
            no_topup += 1
            continue
        if sr and sr in refs and refs[sr]["kind"] == "SR":
            code = sr
        elif sd and sd in refs and refs[sd]["kind"] == "SD":
            code = sd
        else:
            no_ref += 1
            continue
        ref = refs[code]
        kind = ref["kind"]
        adm_day = parse_day(g(c, "ADMISSION_DATE"))
        tarif_rs = to_number(g(c, "TARIF_RS"))
        sep = g(c, "SEP")
        if not sep or adm_day is None or tarif_rs <= 0 or tarif_rs >= MAX_NUMBER:
            bad += 1
            continue
        if sep in seen:  # the same SEP twice (a re-export) would count as a repeat and inflate volume
            dup += 1
            continue
        seen.add(sep)
        if g(c, "NOKARTU"):
            cards.add(g(c, "NOKARTU"))
        rows.append({
            "sep": _clip(sep, 40),
            "kode_rs": _clip(g(c, "KODE_RS"), 20),
            "kelas_rs": _clip(g(c, "KELAS_RS"), 8),
            "kode_tarif": _clip(g(c, "KODE_TARIF"), 8),
            "adm_date": _clip(g(c, "ADMISSION_DATE"), 20),
            "dis_date": _clip(g(c, "DISCHARGE_DATE"), 20),
            "adm_day": adm_day,
            "inacbg": _clip(g(c, "INACBG"), 20),
            "inacbg_desc": _clip(g(c, "DESKRIPSI_INACBG"), 200),
            "diag": _clip(g(c, "DIAGLIST"), 200),
            "proc": _clip(g(c, "PROCLIST"), 200),
            "cmg_code": code,
            "cmg_final": _clip(g(c, kind), 16),
            "cmg_kind": kind,
            "cmg_desc": ref["description"],
            "topup": int(to_number(g(c, "TARIF_" + kind))),
            "tarif_inacbg": int(to_number(g(c, "TARIF_INACBG"))),
            "total_tarif": int(to_number(g(c, "TOTAL_TARIF"))),
            "tarif_rs": int(tarif_rs),
            "los": min(100_000, max(0, int(to_number(g(c, "LOS"))))),
            "card_token": tokenise(g(c, "NOKARTU")),
            "ref_price": int(ref["price"]),
        })
    return {"rows": rows, "episodes": _episodes(lines, g, cards, tokenise), "total": len(lines) - 1,
            "no_topup": no_topup, "no_ref": no_ref, "bad": bad + dup, "dup": dup}


def _episodes(lines: list[str], g: Callable, cards: set[str], tokenise: Callable[[str], str]) -> list[dict]:
    """Second pass: every claim (first row per SEP) whose card has a kept top-up claim."""
    out: list[dict] = []
    if not cards:
        return out
    seen: set[str] = set()
    for line in lines[1:]:
        c = line.split("\t")
        card = g(c, "NOKARTU")
        sep = g(c, "SEP")
        if card not in cards or not sep or sep in seen:
            continue
        adm = parse_day(g(c, "ADMISSION_DATE"))
        if adm is None:
            continue
        seen.add(sep)
        los = min(100_000, max(0, int(to_number(g(c, "LOS")))))
        inacbg = _clip(g(c, "INACBG"), 20)
        inpatient = baseline.is_inpatient(inacbg, los)
        dis = parse_day(g(c, "DISCHARGE_DATE"))
        dis = max(adm, dis if dis is not None else adm + los) if inpatient else adm
        out.append({
            "sep": _clip(sep, 40), "card_token": tokenise(card), "adm_day": adm, "dis_day": dis,
            "inpatient": inpatient, "dx3": baseline.primary_dx(g(c, "DIAGLIST")), "kode_rs": _clip(g(c, "KODE_RS"), 20),
            "adm_date": _clip(g(c, "ADMISSION_DATE"), 20), "dis_date": _clip(g(c, "DISCHARGE_DATE"), 20),
            "topup": bool(norm_code(g(c, "IN_SR")) or norm_code(g(c, "SR")) or norm_code(g(c, "IN_SD")) or norm_code(g(c, "SD"))),
        })
    return out

"""Reader for the E-Klaim INA-CBG TXT export.

Layout: one header line and one line per SEP, tab-separated, as listed for the
pull_claim method in Petunjuk Teknis E-Klaim 5.1.1. Columns are matched by header
name, so extra or reordered columns are fine.

NAMA_PASIEN, MRN and BIRTH_DATE are never read. NOKARTU is turned into a one-way
token before the row leaves this module.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Callable, Optional

NEEDED = ["KODE_RS", "KODE_TARIF", "ADMISSION_DATE", "INACBG", "TARIF_RS", "LOS", "SEP", "NOKARTU"]
KIND_NAMES = {"SR": "Special Prosthesis", "SD": "Special Drug"}

_DMY = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})")
_YMD = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})")
_CODE = re.compile(r"^([A-Z]{2})-?(\d{2})")
_NOT_NUM = re.compile(r"[^0-9.\-]")


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
    s = _NOT_NUM.sub("", value or "")
    try:
        return float(s)
    except ValueError:
        return 0.0


def _clip(value: str, n: int) -> str:
    return (value or "").strip()[:n]


def parse_eklaim(text: str, refs: dict[str, dict], tokenise: Callable[[str], str]) -> dict:
    """Return {"rows": [...], "total", "no_topup", "no_ref", "bad"}.

    `refs` maps a Special CMG code to {"kind", "description", "price"}. Only claims
    whose Special Prosthesis or Special Drug code has a reference price are kept.
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
    no_topup = no_ref = bad = 0
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
        if not sep or adm_day is None or tarif_rs <= 0:
            bad += 1
            continue
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
            "los": max(0, int(to_number(g(c, "LOS")))),
            "card_token": tokenise(g(c, "NOKARTU")),
            "ref_price": int(ref["price"]),
        })
    return {"rows": rows, "total": len(lines) - 1, "no_topup": no_topup, "no_ref": no_ref, "bad": bad}

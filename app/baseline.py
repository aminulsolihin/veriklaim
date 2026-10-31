"""National norms per primary diagnosis, from the BPJS sample data (FKRTL hospital visits).

The file app/data/dx_baseline.json is built by tools/build_dx_baseline.py from the transaction
knowledge base. It holds aggregates only: per 3-character ICD-10 code and care setting, the length of
stay and how many other visits a patient with that diagnosis has within the repeat window.

Two uses in the top-up screen (scoring.py):
  * the stay median of a small INA-CBG group is pulled towards the national median for the diagnosis;
  * repeats are counted above what the diagnosis normally has, so a thalassaemia patient seen every
    few weeks is not read as repeat billing, and a second myocardial infarction claim still is.
A claim whose diagnosis is not in the file is screened exactly as before.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Optional

PATH = Path(__file__).resolve().parent / "data" / "dx_baseline.json"

_DX = re.compile(r"[A-Z]\d\d")
_SPLIT = re.compile(r"[#;,|\s]+")
_CASE = re.compile(r"^[A-Z]-(\d)-")
# INA-CBG case type, the second part of the code: 1 procedure, 4 non-procedure, 6 obstetric and 8 neonatal
# are inpatient; 2, 3, 5, 7 and 9 are outpatient.
_INPATIENT_CASES = frozenset("1468")


@lru_cache(maxsize=1)
def load() -> dict:
    try:
        return json.loads(PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"inpatient": {}, "outpatient": {}}


def primary_dx(diaglist: str) -> str:
    """'I21.0#I10' -> 'I21'. '' when the first code is not an ICD-10 code."""
    first = _SPLIT.split((diaglist or "").strip().upper(), 1)[0]
    m = _DX.match(first)
    return m.group(0) if m else ""


def is_inpatient(inacbg: str, los: int) -> bool:
    m = _CASE.match((inacbg or "").strip().upper())
    return m.group(1) in _INPATIENT_CASES if m else los >= 1


def norm(dx: str, inpatient: bool) -> Optional[dict]:
    if not dx:
        return None
    return load().get("inpatient" if inpatient else "outpatient", {}).get(dx)


def source() -> dict:
    """What the page says about where the norms come from."""
    d = load()
    return {"period": d.get("period"), "visits": d.get("visits"), "diagnoses": len(d.get("inpatient", {})) + len(d.get("outpatient", {}))}

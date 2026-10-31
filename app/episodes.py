"""Record rules on episodes of care: overlapping stays and quick readmissions with the same diagnosis.

A top-up claim is compared with every other claim of the same card: in the same file (any claim, with or
without a top-up, see eklaim._episodes) and in earlier batches of the workspace (the stored top-up claims).

  overlap      two stays of one patient that share a night, or a same-day claim on a day strictly inside a
               stay. A patient cannot lie in two beds at once, so at least one of the two claims is wrong (a
               phantom stay, a double bill, or a wrong date): the claim joins the review queue. Admitted on the
               day the other stay ended is a transfer between hospitals and does not count.
  readmission  admitted 1 to READMIT_DAYS days after a stay with the same primary diagnosis ended. Context
               only, shown with the national rate for the diagnosis (baseline.py): a quick readmission can be
               a relapse, or one episode split into two claims. It does not queue the claim.

On the BPJS sample data (5.4 million hospital visits, tools/build_realdata_eval.py) the overlap rule holds for
777 of 526,011 inpatient stays (about 15 in 10,000), while same-day transfers (6,030) are about 8 times as
common and are left alone.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Iterable, Optional

from . import baseline

READMIT_DAYS = 7
MAX_LISTED = 5   # other claims kept per claim for the page


def overlaps(a0: int, a1: int, b0: int, b1: int) -> bool:
    """Stays [a0, a1] and [b0, b1] in days. A same-day claim has a0 == a1."""
    if max(a0, b0) < min(a1, b1):
        return True
    return (a0 == a1 and b0 < a0 < b1) or (b0 == b1 and a0 < b0 < a1)


def _brief(e: dict) -> dict:
    return {"sep": e["sep"], "rs": e.get("kode_rs", ""), "adm": e.get("adm_date", ""), "dis": e.get("dis_date", ""),
            "dx": e.get("dx3", ""), "topup": bool(e.get("topup")), "batch": e.get("batch")}


def annotate(rows: list[dict], episodes: Iterable[dict], earlier: Iterable[dict] = ()) -> dict:
    """Set overlap_n, overlap_with, readmit_days, readmit_with and readmit_rate on each row, in place.

    `episodes` are the file's claims (eklaim._episodes), `earlier` the stored claims of earlier batches with
    the same card tokens (each with "batch"). Returns counts for the audit entry."""
    episodes = list(episodes)
    own = {e["sep"]: e for e in episodes}
    by_card: dict[str, list[dict]] = defaultdict(list)
    for e in episodes:
        by_card[e["card_token"]].append(e)
    for e in earlier:
        by_card[e["card_token"]].append(e)
    out = {"overlap": 0, "readmit": 0}
    for r in rows:
        r["overlap_n"], r["overlap_with"] = 0, None
        r["readmit_days"], r["readmit_with"], r["readmit_rate"] = None, None, None
        me = own.get(r["sep"])
        if me is None or not r.get("card_token"):
            continue
        hits: dict[str, dict] = {}
        best: Optional[tuple] = None
        for o in by_card[r["card_token"]]:
            if o["sep"] == me["sep"] or o["sep"] in hits:
                continue
            if overlaps(me["adm_day"], me["dis_day"], o["adm_day"], o["dis_day"]):
                hits[o["sep"]] = o
            elif me["inpatient"] and o["inpatient"] and me["dx3"] and o["dx3"] == me["dx3"]:
                gap = me["adm_day"] - o["dis_day"]
                if 1 <= gap <= READMIT_DAYS and (best is None or gap < best[0]):
                    best = (gap, o)
        if hits:
            r["overlap_n"] = len(hits)
            r["overlap_with"] = [_brief(o) for o in sorted(hits.values(), key=lambda o: o["adm_day"])[:MAX_LISTED]]
            out["overlap"] += 1
        if best is not None:
            r["readmit_days"], r["readmit_with"] = best[0], _brief(best[1])
            nat = baseline.norm(me["dx3"], True)
            r["readmit_rate"] = nat.get("readmit") if nat else None
            out["readmit"] += 1
    return out

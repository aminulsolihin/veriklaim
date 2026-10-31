"""Measure VeriKlaim's record rules on real data: the BPJS sample data (FKRTL) in the transaction knowledge base.

Writes app/data/realdata.json, shown on the Ketahanan page. The rules are the ones app/episodes.py applies to
an E-Klaim file, written here in SQL over every hospital visit in the sample:

  overlap            an inpatient stay that shares a night with another stay of the same patient (either one
                     starts before the other has ended, or both start the same day); both stays count, as
                     VeriKlaim queues both claims
  outpatient inside  an outpatient visit on a day strictly inside an inpatient stay of the same patient
  same-day transfer  admitted the day the previous stay ended: a referral, NOT flagged
  readmission        admitted 1 to 7 days after a stay with the same primary diagnosis ended: context, not flagged

Only counts go out, through the knowledge base's privacy layer (guarded read-only SQL, small groups suppressed).

    <claude_kb>\\.venv\\Scripts\\python.exe tools/build_realdata_eval.py --kb <claude_kb>
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "app" / "data" / "realdata.json"

VISITS = """
v AS (
  SELECT entity_token e, julianday(date) ja, julianday(json_extract(extra_json,'$.FKL04')) jf,
         substr(category,1,3) dx, json_extract(extra_json,'$.FKL06') kab
  FROM anon_txn
  WHERE entity_token IS NOT NULL AND json_extract(extra_json,'$.FKL04') IS NOT NULL)"""

STAYS = """
WITH {visits},
ip AS (SELECT * FROM v WHERE jf > ja AND jf - ja <= 365),
w AS (
  SELECT e, ja, jf, dx, kab,
    MAX(jf) OVER (PARTITION BY e ORDER BY ja RANGE BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) open_until,
    MIN(ja) OVER (PARTITION BY e ORDER BY ja RANGE BETWEEN 1 FOLLOWING AND UNBOUNDED FOLLOWING) next_start,
    COUNT(*) OVER (PARTITION BY e ORDER BY ja RANGE BETWEEN CURRENT ROW AND CURRENT ROW) same_adm,
    COUNT(*) OVER (PARTITION BY e, ja, jf, dx) same_stay,
    LAG(jf) OVER (PARTITION BY e ORDER BY ja, jf) prev_dis,
    LAG(kab) OVER (PARTITION BY e ORDER BY ja, jf) prev_kab,
    LAG(jf) OVER (PARTITION BY e, dx ORDER BY ja, jf) prev_dis_dx
  FROM ip)
SELECT COUNT(*) AS n_txn,
  SUM(open_until > ja OR same_adm > 1 OR next_start < jf) AS overlap,
  SUM(open_until > ja) AS overlap_started_inside,
  SUM(same_adm > 1) AS overlap_same_admission_day,
  SUM(prev_dis > ja AND prev_kab <> kab) AS overlap_prev_other_district,
  SUM(same_stay > 1) AS identical_stay,
  SUM(prev_dis = ja) AS same_day_transfer,
  SUM(ja - prev_dis_dx BETWEEN 1 AND 7) AS readmit7_same_dx,
  SUM(ja - prev_dis_dx BETWEEN 1 AND 30) AS readmit30_same_dx
FROM w"""

OUTPATIENT = """
WITH {visits},
x AS (SELECT e, ja, jf, (jf > ja) ip FROM v WHERE jf >= ja AND jf - ja <= 365),
w AS (
  SELECT ip, ja,
    MAX(CASE WHEN ip THEN jf END) OVER (PARTITION BY e ORDER BY ja RANGE BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) open_until
  FROM x)
SELECT SUM(NOT ip) AS n_txn, SUM(NOT ip AND open_until > ja) AS outpatient_inside_stay FROM w"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--kb", required=True, help="folder of the transaction knowledge base (holds kb/ and config.py)")
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()
    sys.path.insert(0, str(Path(args.kb).resolve()))
    from kb import analytics, db  # noqa: E402  (the knowledge base's own privacy layer)
    db.init()

    period = analytics.overview()["rows"][0]
    res = {}
    for name, sql in (("stays", STAYS), ("outpatient", OUTPATIENT)):
        t0 = time.time()
        r = analytics.run_sql(sql.format(visits=VISITS))
        res[name] = r["rows"][0]
        print("{}: {} in {:.0f}s ({})".format(name, res[name], time.time() - t0, r["privacy"]))
    s, o = res["stays"], res["outpatient"]
    doc = {
        "source": "Data Sampel BPJS Kesehatan, FKRTL (kunjungan rumah sakit), lewat knowledge base transaction-kb",
        "period": [period["first_date"], period["last_date"]],
        "visits": period["n_txn"],
        "built": date.today().isoformat(),
        "inpatientStays": s["n_txn"],
        "outpatientVisits": o["n_txn"],
        "overlap": s["overlap"],
        "overlapStartedInside": s["overlap_started_inside"],
        "overlapSameAdmissionDay": s["overlap_same_admission_day"],
        "overlapPrevOtherDistrict": s["overlap_prev_other_district"],
        "identicalStay": s["identical_stay"],
        "outpatientInsideStay": o["outpatient_inside_stay"],
        "sameDayTransfer": s["same_day_transfer"],
        "readmit7SameDx": s["readmit7_same_dx"],
        "readmit30SameDx": s["readmit30_same_dx"],
    }
    Path(args.out).write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print("wrote", args.out)


if __name__ == "__main__":
    main()

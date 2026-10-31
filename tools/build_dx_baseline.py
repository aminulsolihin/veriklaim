"""Build app/data/dx_baseline.json from the transaction knowledge base (BPJS sample data, FKRTL).

The top-up screen compares a claim only with the other claims in the same file. Two things a file
cannot tell it are how long a stay for this diagnosis normally is (when the file has few claims in
the INA-CBG group) and how often a patient with this diagnosis normally comes back. A thalassaemia
patient on iron chelation is seen every few weeks, a patient with a myocardial infarction is not, and
without that the "same card, same top-up, 90 days" feature treats both the same.

This script asks the knowledge base (C:\\...\\claude_kb, see its README) for national norms per
3-character ICD-10 primary diagnosis and care setting. It goes through the knowledge base's own
privacy layer (guarded read-only SQL on the pseudonymised table, groups below k suppressed), and only
groups of at least MIN_N visits are written. The output holds aggregates only, no visit or person.

    <claude_kb>\\.venv\\Scripts\\python.exe tools/build_dx_baseline.py --kb <claude_kb>

Dates. Admission is the knowledge base's visit date (FKL03), discharge is FKL04 as written in the file.
Knowledge bases filled before the date fix of 2026-10-31 hold about 13% of visit dates with day and month
swapped; run `kb_cli.py repair-dates` there first. Repeats are ordered by the discharge date.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "app" / "data" / "dx_baseline.json"
MIN_N = 30
WINDOW = 90   # days either side, as scoring.REPEAT_WINDOW_DAYS
READMIT = 7   # days after discharge, as episodes.READMIT_DAYS
PAGES = 2     # 200 rows per page (the knowledge base's row cap)

# Primary diagnoses behind the Special CMG top-ups (Permenkes 26/2021), always included:
# thalassaemia and other anaemias (chelators), haemophilia and coagulation (factor), myocardial
# infarction (streptokinase), sepsis and shock (albumin), burns (albumin), hip and knee, femur fracture,
# osteomyelitis (implants), aneurysm (EVAR/TEVAR), epilepsy (subdural grid), TMJ, middle ear (graft).
TOPUP_DX = ["D56", "D57", "D58", "D61", "D64", "D66", "D67", "D68", "D69", "I21", "I22", "A40", "A41", "R57",
            "T20", "T21", "T22", "T23", "T24", "T25", "T29", "T30", "T31", "T32", "M16", "M17", "S72", "M87",
            "I71", "I72", "G40", "M26", "H71", "H66", "H90"]

SQL = """
WITH u AS (
  SELECT entity_token e, substr(category,1,3) dx, amount a, date adm,
         json_extract(extra_json,'$.FKL04') f
  FROM anon_txn WHERE category IS NOT NULL AND json_extract(extra_json,'$.FKL04') IS NOT NULL),
v AS (SELECT e, dx, a, julianday(adm) ja, julianday(f) jf, CAST(julianday(f)-julianday(adm) AS INT) los FROM u),
w AS (
  SELECT dx, a, los, (los>=1) ip,
    jf - LAG(jf) OVER (PARTITION BY e, dx, (los>=1) ORDER BY jf) gap,
    ja - LAG(jf) OVER (PARTITION BY e, dx, (los>=1) ORDER BY ja, jf) rgap,
    COUNT(*) OVER (PARTITION BY e, dx, (los>=1) ORDER BY jf
                   RANGE BETWEEN {w} PRECEDING AND {w} FOLLOWING) - 1 near
  FROM v WHERE los BETWEEN 0 AND 365),
r AS (
  SELECT dx, ip, a, los, gap, rgap, near,
    ROW_NUMBER() OVER (PARTITION BY dx, ip ORDER BY los) rl,
    ROW_NUMBER() OVER (PARTITION BY dx, ip ORDER BY a) ra,
    ROW_NUMBER() OVER (PARTITION BY dx, ip ORDER BY near) rn,
    COUNT(*) OVER (PARTITION BY dx, ip) c
  FROM w)
SELECT dx, ip, COUNT(*) AS n_txn,
  MAX(CASE WHEN rl=(c+1)/2 THEN los END) los_p50,
  MAX(CASE WHEN rl=(c*3+3)/4 THEN los END) los_p75,
  MAX(CASE WHEN rl=(c*9+9)/10 THEN los END) los_p90,
  MAX(CASE WHEN ra=(c+1)/2 THEN a END) amt_p50,
  ROUND(COALESCE(SUM(gap<={w}),0)*1.0/COUNT(*),4) rep_prior,
  ROUND(COALESCE(SUM(rgap BETWEEN 1 AND {readmit}),0)*1.0/COUNT(*),4) readmit,
  ROUND(AVG(near),3) near_mean,
  MAX(CASE WHEN rn=(c*9+9)/10 THEN near END) near_p90
FROM r GROUP BY dx, ip HAVING n_txn>={min_n}
ORDER BY dx IN ({topup}) DESC, ip DESC, n_txn DESC
LIMIT 200 OFFSET {offset}
"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--kb", required=True, help="folder of the transaction knowledge base (holds kb/ and config.py)")
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()
    sys.path.insert(0, str(Path(args.kb).resolve()))
    from kb import analytics, db  # noqa: E402  (the knowledge base's own privacy layer)
    db.init()

    period = analytics.overview()["rows"][0]
    topup = ",".join("'{}'".format(d) for d in TOPUP_DX)
    rows: list[dict] = []
    for page in range(PAGES):
        t0 = time.time()
        res = analytics.run_sql(SQL.format(w=WINDOW, readmit=READMIT, min_n=MIN_N, topup=topup, offset=200 * page))
        rows += res["rows"]
        print("page {}: {} groups in {:.0f}s ({})".format(page + 1, len(res["rows"]), time.time() - t0, res["privacy"]))
        if len(res["rows"]) < 200:
            break

    out: dict[str, dict] = {"inpatient": {}, "outpatient": {}}
    for r in rows:
        item = {"n": r["n_txn"], "rep_prior": r["rep_prior"], "near_mean": r["near_mean"], "near_p90": r["near_p90"],
                "amt_p50": r["amt_p50"]}
        if r["ip"]:
            item.update(los_p50=r["los_p50"], los_p75=r["los_p75"], los_p90=r["los_p90"], readmit=r["readmit"])
        out["inpatient" if r["ip"] else "outpatient"][r["dx"]] = item
    doc = {
        "source": "Data Sampel BPJS Kesehatan, FKRTL (kunjungan rumah sakit), lewat knowledge base transaction-kb",
        "period": [period["first_date"], period["last_date"]],
        "visits": period["n_txn"],
        "built": date.today().isoformat(),
        "window_days": WINDOW,
        "readmit_days": READMIT,
        "min_n": MIN_N,
        "fields": {
            "n": "kunjungan dalam kelompok (diagnosis primer 3 karakter x rawat inap/jalan)",
            "los_p50/p75/p90": "lama rawat (hari) persentil 50/75/90, rawat inap saja",
            "rep_prior": "bagian kunjungan yang didahului kunjungan lain pasien yang sama, diagnosis sama, <= window_days hari",
            "near_mean": "rata-rata kunjungan lain pasien yang sama, diagnosis sama, dalam +/- window_days hari",
            "near_p90": "persentil 90 dari jumlah yang sama",
            "amt_p50": "median biaya klaim (FKL48), rupiah",
            "readmit": "rawat inap saja: bagian rawat inap yang dimulai 1..readmit_days hari setelah rawat inap pasien yang sama, diagnosis sama, selesai",
        },
        "caveats": [
            "Diagnosis primer FKL17 dipotong ke 3 karakter; tidak ada kode INA-CBG atau tingkat keparahan di data ini.",
            "Rawat inap = tanggal pulang (FKL04) setelah tanggal masuk (FKL03).",
            "Pengulangan dihitung per diagnosis, bukan per item top-up: ini pola kunjungan, bukan pola pemberian obat.",
        ],
        **out,
    }
    Path(args.out).write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print("wrote {}: {} inpatient, {} outpatient diagnoses".format(args.out, len(out["inpatient"]), len(out["outpatient"])))


if __name__ == "__main__":
    main()

"""Time the whole read-and-score path on a large synthetic E-Klaim file.

    python tools/benchmark.py 1530000 0.03     # 1.53 million rows, 3% with a top-up
    python tools/benchmark.py 1000000 1.0      # 1 million rows, every row a top-up

Reported: parse time, score + rank + explain time and peak memory. Database writes are not included.
Numbers in the deck (2 vCPU, 8 GB): 1.53 million rows, 3% top-up: 6.5 s, 1.07 GB;
1 million rows, all top-up: 38.7 s, 2.2 GB. Synthetic data only.
"""
import sys, time, random, resource
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import eklaim, scoring
from app.security import card_token

HEAD = "KODE_RS KELAS_RS KELAS_RAWAT KODE_TARIF PTD ADMISSION_DATE DISCHARGE_DATE BIRTH_DATE BIRTH_WEIGHT SEX DISCHARGE_STATUS DIAGLIST PROCLIST ADL1 ADL2 IN_SP IN_SR IN_SI IN_SD INACBG SUBACUTE CHRONIC SP SR SI SD DESKRIPSI_INACBG TARIF_INACBG TARIF_SUBACUTE TARIF_CHRONIC DESKRIPSI_SP TARIF_SP DESKRIPSI_SR TARIF_SR DESKRIPSI_SI TARIF_SI DESKRIPSI_SD TARIF_SD TOTAL_TARIF TARIF_RS LOS ICU_INDIKATOR ICU_LOS VENT_HOUR NAMA_PASIEN MRN UMUR_TAHUN UMUR_HARI DPJP SEP NOKARTU PAYOR_ID CODER_ID VERSI_INACBG VERSI_GROUPER C1 C2 C3 C4".split()
assert len(HEAD) == 59
col = {h: i for i, h in enumerate(HEAD)}
refs = {"RR04": {"kind": "SR", "description": "Hip implant / knee implant", "price": 25_000_000},
        "DD01": {"kind": "SD", "description": "Streptokinase", "price": 4_000_000}}

def make(n, topup_share, seed=1):
    rnd = random.Random(seed)
    out = ["\t".join(HEAD)]
    cards = [str(9990000000000 + i) for i in range(max(1000, n // 3))]
    cbgs = ["M-1-04-I", "M-1-04-II", "M-1-04-III", "I-4-10-I", "I-4-10-II", "I-4-10-III", "K-1-10-I", "O-6-10-II"]
    for i in range(n):
        c = ["-"] * 59
        c[col["KODE_RS"]] = str(9990000 + rnd.randrange(600))
        c[col["KELAS_RS"]] = "C"; c[col["KODE_TARIF"]] = "CP"
        d = rnd.randrange(1, 28)
        c[col["ADMISSION_DATE"]] = f"{d:02d}/{rnd.randrange(1,13):02d}/2026"
        c[col["DISCHARGE_DATE"]] = f"{d:02d}/{rnd.randrange(1,13):02d}/2026"
        c[col["BIRTH_DATE"]] = "01/01/1960"; c[col["NAMA_PASIEN"]] = f"PASIEN SINTETIS {i}"; c[col["MRN"]] = str(100000 + i)
        for k in ("IN_SP", "IN_SR", "IN_SI", "IN_SD", "SR", "SD", "SP", "SI"): c[col[k]] = "None"
        if rnd.random() < topup_share:
            if rnd.random() < 0.7:
                c[col["IN_SR"]] = "RR04"; c[col["SR"]] = "RR-04-III"; c[col["TARIF_SR"]] = "26197900"
            else:
                c[col["IN_SD"]] = "DD01"; c[col["SD"]] = "DD-01-II"; c[col["TARIF_SD"]] = "4596000"
        c[col["INACBG"]] = rnd.choice(cbgs)
        c[col["TARIF_INACBG"]] = "33228600"; c[col["TOTAL_TARIF"]] = "72525500"
        c[col["TARIF_RS"]] = str(rnd.randrange(8_000_000, 90_000_000))
        c[col["LOS"]] = str(rnd.randrange(1, 14))
        c[col["SEP"]] = f"9990R00{i:09d}"; c[col["NOKARTU"]] = rnd.choice(cards)
        out.append("\t".join(c))
    return "\n".join(out)

def run(n, share):
    t0 = time.perf_counter(); txt = make(n, share); tg = time.perf_counter() - t0
    mb = len(txt) / 1e6
    t0 = time.perf_counter(); res = eklaim.parse_eklaim(txt, refs, card_token); tp = time.perf_counter() - t0
    rows = res["rows"]
    t0 = time.perf_counter(); m = scoring.screen(rows); ts = time.perf_counter() - t0
    print(f"claims_in_file={n:>9,} file={mb:7.1f}MB topup_rows={len(rows):>8,} parse={tp:6.1f}s score+rank+explain={ts:6.1f}s total={tp+ts:6.1f}s peakRSS={resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024:6.0f}MB", flush=True)

if __name__ == "__main__":
    n = int(sys.argv[1]); share = float(sys.argv[2])
    run(n, share)

"""Make the synthetic upcoding sample: an E-Klaim TXT with planted upcoding.

    python tools/make_coding_sample.py                 # writes the three files the app ships
    from tools.make_coding_sample import generate      # generate(seed) -> (text, labels)

Everything is invented. Case groups, tariffs, stays and diagnosis counts follow rules chosen by
the author, and the planted upcoding is injected by those same rules, so how well the check finds
it says only that the code does what it was written to do. It is not an estimate of how well it
would do on real claims.

Planted patterns (the answer key goes to app/data/sample-coding-labels.json):
  strong    three hospitals code a large share of claims that show the care of level I as II or III
  mild      one hospital does so for a smaller share and stretches those stays a little
  referral  two class A hospitals really do treat sicker patients: more II/III, longer stays, more ICU
  tiny      one hospital with too few claims for a hospital-level comparison
  normal    everyone else
"""
from __future__ import annotations

import json
import math
import random
import sys
from pathlib import Path

HEAD = ("KODE_RS KELAS_RS KELAS_RAWAT KODE_TARIF PTD ADMISSION_DATE DISCHARGE_DATE BIRTH_DATE BIRTH_WEIGHT SEX "
        "DISCHARGE_STATUS DIAGLIST PROCLIST ADL1 ADL2 IN_SP IN_SR IN_SI IN_SD INACBG SUBACUTE CHRONIC SP SR SI SD "
        "DESKRIPSI_INACBG TARIF_INACBG TARIF_SUBACUTE TARIF_CHRONIC DESKRIPSI_SP TARIF_SP DESKRIPSI_SR TARIF_SR "
        "DESKRIPSI_SI TARIF_SI DESKRIPSI_SD TARIF_SD TOTAL_TARIF TARIF_RS LOS ICU_INDIKATOR ICU_LOS VENT_HOUR "
        "NAMA_PASIEN MRN UMUR_TAHUN UMUR_HARI DPJP SEP NOKARTU PAYOR_ID CODER_ID VERSI_INACBG VERSI_GROUPER "
        "C1 C2 C3 C4").split()
assert len(HEAD) == 59
COL = {h: i for i, h in enumerate(HEAD)}

# code, base tariff at level I (Rp), median stay at level I (days), P(true level I / II / III), weight
GROUPS = [
    ("A-4-10", 3_200_000, 4, (.60, .28, .12), 1.0),
    ("E-4-10", 5_100_000, 5, (.50, .32, .18), 1.0),
    ("G-4-10", 4_400_000, 4, (.55, .30, .15), 1.0),
    ("I-4-10", 6_800_000, 5, (.45, .32, .23), 1.0),
    ("J-4-10", 3_900_000, 4, (.62, .26, .12), 1.2),
    ("K-4-10", 5_500_000, 5, (.52, .30, .18), 1.0),
    ("L-4-10", 3_000_000, 3, (.68, .24, .08), 1.2),
    ("M-4-10", 4_100_000, 4, (.58, .28, .14), 1.0),
    ("N-4-10", 5_900_000, 6, (.40, .35, .25), 0.8),
    ("O-6-10", 2_600_000, 3, (.72, .22, .06), 1.2),
    ("Q-4-10", 4_600_000, 4, (.54, .30, .16), 1.0),
    ("S-4-10", 3_700_000, 4, (.64, .25, .11), 1.0),
]
HEAVY = {"I-4-10", "N-4-10", "E-4-10", "K-4-10"}   # referral hospitals see more of these
TARIF_STEP = (1.00, 1.55, 2.35)
CLASS_FACTOR = {"A": 1.12, "B": 1.00, "C": 0.90, "D": 0.82}
STAY_STEP = (1.0, 1.55, 2.4)
SEC_MEAN = (0.9, 2.5, 4.2)
ICU_P = (0.02, 0.12, 0.38)
VENT_P = (0.10, 0.25, 0.50)         # given ICU
DIED_P = (0.01, 0.04, 0.11)
WORD = ("RINGAN", "SEDANG", "BERAT")
PRINCIPAL = {"A-4-10": "A09", "E-4-10": "J18.9", "G-4-10": "K29.7", "I-4-10": "I21.0", "J-4-10": "N39.0",
             "K-4-10": "K35.8", "L-4-10": "R55", "M-4-10": "M54.5", "N-4-10": "N17.9", "O-6-10": "O80",
             "Q-4-10": "E11.9", "S-4-10": "S06.0"}
SECONDARY = ["I10", "E11.9", "E78.5", "N18.9", "J44.9", "K21.9", "D64.9", "E87.1", "I50.9", "R50.9", "E66.9",
             "G47.3", "M10.9", "N40", "K76.0", "E03.9", "I25.1", "J45.9", "L03.1", "F17.2", "E87.6", "D50.9",
             "I48.9", "N39.0", "R06.0", "A41.9", "J96.0", "K92.2", "E83.4", "E16.2"]

# role, claims, share of true-level-I claims coded higher, class
HOSPITALS = (
    [("strong", 64, .38, "C"), ("strong", 58, .32, "B"), ("strong", 60, .28, "C"),
     ("mild", 62, .14, "B"),
     ("referral", 60, 0, "A"), ("referral", 56, 0, "A"),
     ("tiny", 12, .34, "D")]
    + [("normal", n, 0, c) for n, c in zip(
        [42, 38, 52, 44, 36, 48, 40, 34, 46, 50, 39, 45, 41, 37, 47, 43, 33],
        "BCCDBCDCBBCDCBCBD")]
)


def _poisson(rnd: random.Random, lam: float) -> int:
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rnd.random()
        if p <= limit:
            return k
        k += 1


def _level(rnd: random.Random, probs) -> int:
    x, acc = rnd.random(), 0.0
    for i, p in enumerate(probs):
        acc += p
        if x < acc:
            return i + 1
    return len(probs)


def generate(seed: int = 11, strength: float = 1.0) -> tuple[str, dict]:
    """Return (TXT text, labels). `strength` scales the planted upcoding shares (1.0 = as shipped)."""
    rnd = random.Random(seed)
    lines = ["\t".join(HEAD)]
    claims: dict[str, str] = {}
    roles: dict[str, str] = {}
    seq = 0
    for h_i, (role, n, p_up, kelas) in enumerate(HOSPITALS):
        rs = f"9991{h_i + 1:03d}"
        roles[rs] = role
        for _ in range(n):
            seq += 1
            weights = [g[4] * (2.2 if role == "referral" and g[0] in HEAVY else 1.0) for g in GROUPS]
            code, tarif1, los1, probs, _w = rnd.choices(GROUPS, weights=weights)[0]
            if role == "referral":
                probs = tuple(p * m for p, m in zip(probs, (0.55, 1.5, 1.9)))
                probs = tuple(p / sum(probs) for p in probs)
            true = _level(rnd, probs)
            coded, planted = true, False
            if true == 1 and p_up and rnd.random() < p_up * strength:
                coded, planted = (2 if rnd.random() < 0.65 else 3), True
            # care follows the true level; the coded diagnoses follow the coded level
            stay = los1 * STAY_STEP[true - 1] * (1.15 if role == "referral" else 1.0)
            if planted and role == "mild":
                stay *= 1.3
            los = max(1, round(stay * math.exp(rnd.gauss(0, 0.35))))
            icu_p = ICU_P[true - 1] * (1.5 if role == "referral" else 1.0)
            icu = rnd.random() < icu_p
            icu_los = rnd.randint(1, min(5, los)) if icu else 0
            vent = rnd.randrange(6, 121) if icu and rnd.random() < VENT_P[true - 1] else 0
            died = rnd.random() < DIED_P[true - 1]
            if died:
                los = max(1, round(los * rnd.uniform(0.3, 1.0)))
            n_sec = _poisson(rnd, SEC_MEAN[coded - 1])
            diag = "#".join([PRINCIPAL[code]] + rnd.sample(SECONDARY, min(n_sec, len(SECONDARY))))
            month = rnd.randint(1, 6)
            day = rnd.randint(1, 24)
            adm = f"{day:02d}/{month:02d}/2026"
            dis = f"{min(28, day + min(los, 4)):02d}/{month:02d}/2026"
            tarif = int(round(tarif1 * TARIF_STEP[coded - 1] * CLASS_FACTOR[kelas], -2))
            sep = f"{rs}R{h_i + 1:03d}{month:02d}26V{seq:06d}"
            c = ["-"] * 59
            c[COL["KODE_RS"]], c[COL["KELAS_RS"]], c[COL["KELAS_RAWAT"]] = rs, kelas, "3"
            c[COL["KODE_TARIF"]], c[COL["PTD"]] = "CP", "1"
            c[COL["ADMISSION_DATE"]], c[COL["DISCHARGE_DATE"]] = adm, dis
            c[COL["BIRTH_DATE"]], c[COL["BIRTH_WEIGHT"]], c[COL["SEX"]] = "01/01/1960", "0", str(rnd.randint(1, 2))
            c[COL["DISCHARGE_STATUS"]] = "4" if died else "1"
            c[COL["DIAGLIST"]] = diag
            for k in ("IN_SP", "IN_SR", "IN_SI", "IN_SD", "SP", "SR", "SI", "SD", "SUBACUTE", "CHRONIC"):
                c[COL[k]] = "None"
            c[COL["INACBG"]] = f"{code}-{'I' * coded}"
            c[COL["DESKRIPSI_INACBG"]] = f"KASUS SINTETIS {code} ({WORD[coded - 1]})"
            c[COL["TARIF_INACBG"]] = c[COL["TOTAL_TARIF"]] = str(tarif)
            c[COL["TARIF_RS"]] = str(int(round(tarif * rnd.uniform(1.0, 1.6), -2)))
            c[COL["LOS"]] = str(los)
            c[COL["ICU_INDIKATOR"]], c[COL["ICU_LOS"]], c[COL["VENT_HOUR"]] = ("1" if icu else "0"), str(icu_los), str(vent)
            c[COL["NAMA_PASIEN"]], c[COL["MRN"]] = f"PASIEN SINTETIS {seq:04d}", str(500000 + seq)
            c[COL["UMUR_TAHUN"]], c[COL["UMUR_HARI"]] = str(rnd.randint(25, 80)), "0"
            c[COL["DPJP"]] = f"DPJP SINTETIS {rnd.randint(1, 30)}"
            c[COL["SEP"]], c[COL["NOKARTU"]] = sep, str(9994600000000 + seq * 7919)
            c[COL["PAYOR_ID"]], c[COL["CODER_ID"]] = "3;JKN", "000000000"
            c[COL["VERSI_INACBG"]], c[COL["VERSI_GROUPER"]] = "5.1.1", "4"
            lines.append("\t".join(c))
            if planted:
                claims[sep] = "upcoded"
    return "\n".join(lines) + "\n", {"claims": claims, "hospitals": roles}


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    text, labels = generate()
    (root / "app/data/sample-coding.txt").write_text(text, encoding="utf-8")
    (root / "app/data/sample-coding-labels.json").write_text(json.dumps(labels, indent=1), encoding="utf-8")
    out = root / "app/static/assets/sample"
    out.mkdir(parents=True, exist_ok=True)
    (out / "veriklaim-sample-koding.txt").write_text(text, encoding="utf-8")
    n = len(text.splitlines()) - 1
    print(f"{n} claims, {len(labels['claims'])} planted, {len(labels['hospitals'])} hospitals")


if __name__ == "__main__":
    main()

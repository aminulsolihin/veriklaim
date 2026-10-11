"""Robustness check on a larger synthetic set that includes legitimate repeaters.

The 80-claim sample in the deck is clean by construction. This script builds 2,000 top-up claims
(a few hundred ordinary hospitals, two top-up items) and inserts:

  * 8 groups of "problem" repeats (same card, same item, same hospital), at four levels of difficulty:
      L1  3 claims within 31 days, 1-2 day stays   (like the 80-claim sample)
      L2  2 claims within 45 days, 2-3 day stays
      L3  2 claims within 70 days, 3-4 day stays
      L4  2 claims 70 days apart, normal stays and bills (indistinguishable from the next group)
  * about 8% of all claims as LEGITIMATE repeaters (bilateral replacements about 10 weeks apart, or
    three claims 30 days apart) with normal stays, so that the model has hard negatives to confuse.

It then reports recall, precision and the share of legitimate repeaters in the queue for several
review workloads. Everything is synthetic and seeded; nothing here is measured on real claims.

    python tools/stress_test.py                 # 20 seeds, n=2000
    python tools/stress_test.py --seeds 5 --json out.json
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import scoring  # noqa: E402

REF = {"RR04": 25_000_000, "DD01": 4_000_000}
FRACTIONS = [0.01, 0.02, 0.03, 0.05, 0.075, 0.10, 0.15, 0.20]
LEVELS = {1: "3 klaim, rawat 1-2 hari", 2: "2 klaim, rawat 2-3 hari", 3: "2 klaim, rawat 3-4 hari", 4: "2 klaim, rawat normal"}


def generate(seed: int, n: int = 2000, level: int = 1, groups: int = 8, bilateral_rate: float = 0.08):
    """Return (rows, injected_positions, legitimate_repeater_positions). The order of random draws is
    fixed, so the same seed always gives the same data."""
    rnd = random.Random(seed)
    rows, inj = [], set()
    n_hosp = 40
    weight = [rnd.uniform(0.4, 2.0) for _ in range(n_hosp)]

    def add(h, code, card, day, los, cover, tag=None):
        sev = rnd.choice(["I", "II", "II", "III"])
        cbg = ("M-1-04-" if code == "RR04" else "I-4-10-") + sev
        r = {"sep": f"S{len(rows)}", "kode_rs": f"H{h:03d}", "cmg_code": code, "inacbg": cbg, "los": los,
             "tarif_rs": int(REF[code] * cover), "ref_price": REF[code], "card_token": card, "adm_day": day}
        if tag:
            inj.add(len(rows))
        rows.append(r)

    pid = 0

    def normal():
        nonlocal pid
        h = rnd.choices(range(n_hosp), weight)[0]
        code = "RR04" if rnd.random() < 0.55 else "DD01"
        pid += 1
        card = f"P{pid}"
        los = rnd.randint(5, 9) if code == "RR04" else rnd.randint(4, 7)
        cover = rnd.uniform(1.9, 3.0) if code == "RR04" else rnd.uniform(2.2, 4.6)
        add(h, code, card, rnd.randint(0, 84), los, cover)

    # problem groups: same card + same top-up at one hospital, the bill itself looks ordinary
    for _ in range(groups):
        h = rnd.randrange(n_hosp)
        code = rnd.choice(["RR04", "DD01"])
        pid += 1
        card = f"P{pid}"
        if level == 1:
            reps, los_c, span = 3, (1, 2), 31
        elif level == 2:
            reps, los_c, span = 2, (2, 3), 45
        elif level == 3:
            reps, los_c, span = 2, (3, 4), 70
        else:
            reps, los_c, span = 2, ((5, 9) if code == "RR04" else (4, 7)), 70
        d0 = rnd.randint(0, 80 - span)
        for k in range(reps):
            cover = rnd.uniform(1.9, 3.0) if code == "RR04" else rnd.uniform(2.2, 4.6)
            add(h, code, card, d0 + (span * k) // max(1, reps - 1), rnd.randint(*los_c), cover, tag=True)

    # legitimate repeaters (hard negatives): bilateral replacements about 10 weeks apart, or three claims
    # 30 days apart, always with a normal stay
    legit = set()
    target = int(bilateral_rate * n)
    made = 0
    while made < target:
        h = rnd.randrange(n_hosp)
        pid += 1
        card = f"P{pid}"
        code = rnd.choice(["RR04", "DD01"])
        reps, gap = (2, 70) if rnd.random() < 0.6 else (3, 30)
        d0 = rnd.randint(0, 10)
        for k in range(reps):
            los = rnd.randint(5, 9) if code == "RR04" else rnd.randint(4, 7)
            cover = rnd.uniform(1.9, 3.0) if code == "RR04" else rnd.uniform(2.2, 4.6)
            add(h, code, card, d0 + gap * k, los, cover)
            legit.add(len(rows) - 1)
            made += 1

    while len(rows) < n:
        normal()
    return rows, inj, legit


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seeds", type=int, default=20)
    ap.add_argument("--n", type=int, default=2000)
    ap.add_argument("--json", help="write the numbers to this file")
    args = ap.parse_args()

    out = {}
    for level, name in LEVELS.items():
        rec = {f: [] for f in FRACTIONS}
        prec = {f: [] for f in FRACTIONS}
        lg = {f: [] for f in FRACTIONS}
        base = []
        for s in range(args.seeds):
            rows, inj, legit = generate(1000 + s, n=args.n, level=level)
            scoring.screen(rows)
            base.append(len(inj) / len(rows))
            for f in FRACTIONS:
                k = int(f * len(rows))
                queue = {i for i, r in enumerate(rows) if r["rank"] <= k}
                rec[f].append(len(queue & inj) / len(inj))
                prec[f].append(len(queue & inj) / k)
                lg[f].append(len(queue & legit) / len(legit))
        base_rate = statistics.mean(base)
        out[level] = {
            "description": name, "base_rate": round(base_rate, 4),
            "recall": [round(statistics.mean(rec[f]), 3) for f in FRACTIONS],
            "precision": [round(statistics.mean(prec[f]), 3) for f in FRACTIONS],
            "legit_in_queue": [round(statistics.mean(lg[f]), 3) for f in FRACTIONS],
        }
        print(f"\nL{level}  {name}   (proporsi pola dalam data {base_rate:.2%}, {args.seeds} seed, n={args.n})")
        print("  beban      " + "".join(f"{f:>8.1%}" for f in FRACTIONS))
        print("  rekal      " + "".join(f"{v:>8.1%}" for v in out[level]["recall"]))
        print("  presisi    " + "".join(f"{v:>8.1%}" for v in out[level]["precision"]))
        print("  sah di antrean" + "".join(f"{v:>6.0%}" for v in out[level]["legit_in_queue"]))
        i5 = FRACTIONS.index(0.05)
        print(f"  pada beban 5%: rekal {out[level]['recall'][i5]:.1%}, presisi {out[level]['precision'][i5]:.1%} "
              f"= {out[level]['precision'][i5] / base_rate:.1f}x acak")
    if args.json:
        Path(args.json).write_text(json.dumps({"fractions": FRACTIONS, "levels": out, "n": args.n, "seeds": args.seeds}, indent=2))


if __name__ == "__main__":
    main()

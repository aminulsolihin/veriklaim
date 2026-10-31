"""Does the national diagnosis baseline (app/baseline.py) help the top-up screen? An A/B on synthetic data.

Built on stress_test.generate: the same problem groups (L1 to L4) and legitimate bilateral repeaters,
with a primary diagnosis on every claim (hip/knee implant M17, streptokinase I21). Added: thalassaemia
patients on iron chelation (DD02, D56) at a share of the claims, who come back every three to six weeks
with a one or two day stay, as the knowledge base shows for D56 (86% of visits follow another within
90 days, median stay 1 day). Their claims are legitimate.

Each file is screened twice: with the diagnosis (national norms on) and with it removed (the screen as
before this change). Reported at a review workload of 5% of claims, averaged over the seeds.

    python tools/eval_dx_baseline.py                 # 10 seeds
    python tools/eval_dx_baseline.py --seeds 20 --json out.json

Everything here is synthetic. It shows the mechanism works as intended; it is not a measurement on real claims.
"""
from __future__ import annotations

import argparse
import copy
import json
import random
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import scoring  # noqa: E402
from tools.stress_test import generate  # noqa: E402

DIAG = {"RR04": "M17.1", "DD01": "I21.0"}
WORKLOAD = 0.05
SHARES = [0.0, 0.05, 0.10, 0.15]


def with_chronic(seed: int, level: int, share: float, n: int = 2000):
    rows, inj, legit = generate(seed, n=n, level=level)
    for r in rows:
        r["diag"] = DIAG[r["cmg_code"]]
    rnd = random.Random(seed * 7919 + level)
    target = int(share * n)
    # replace ordinary claims (not problem, not bilateral) from the end so positions of the others stay put
    free = [i for i in range(len(rows) - 1, -1, -1) if i not in inj and i not in legit]
    chronic, pid = set(), 0
    while len(chronic) < target and free:
        pid += 1
        h = rnd.randrange(40)
        day = rnd.randint(0, 20)
        while day <= 84 and len(chronic) < target and free:
            i = free.pop(0)
            rows[i] = {"sep": f"T{i}", "kode_rs": f"H{h:03d}", "cmg_code": "DD02", "inacbg": "D-4-13-I",
                       "diag": "D56.1", "los": rnd.randint(1, 2), "ref_price": 3_000_000,
                       "tarif_rs": int(3_000_000 * rnd.uniform(1.6, 3.2)), "card_token": f"T{pid}", "adm_day": day}
            chronic.add(i)
            day += rnd.randint(21, 45)
    return rows, inj, legit, chronic


def queue_stats(rows, inj, legit, chronic):
    scoring.screen(rows)
    k = int(WORKLOAD * len(rows))
    q = {i for i, r in enumerate(rows) if r["rank"] <= k}
    return {"recall": len(q & inj) / len(inj), "precision": len(q & inj) / k,
            "chronic_in_queue": len(q & chronic) / k, "bilateral_in_queue": len(q & legit) / k}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seeds", type=int, default=10)
    ap.add_argument("--json")
    args = ap.parse_args()
    out = []
    print(f"beban tinjau {WORKLOAD:.0%}, {args.seeds} seed, n=2000. sebelum -> sesudah (norma nasional diagnosis)")
    for level in (1, 2, 3, 4):
        for share in SHARES:
            acc = {"before": [], "after": []}
            for s in range(args.seeds):
                rows, inj, legit, chronic = with_chronic(1000 + s, level, share)
                blind = copy.deepcopy(rows)
                for r in blind:
                    r["diag"] = ""
                acc["before"].append(queue_stats(blind, inj, legit, chronic))
                acc["after"].append(queue_stats(rows, inj, legit, chronic))
            m = {k: {f: round(statistics.mean(x[f] for x in v), 3) for f in v[0]} for k, v in acc.items()}
            out.append({"level": level, "chronic_share": share, **m})
            b, a = m["before"], m["after"]
            print(f"L{level} talasemia {share:>4.0%}:  rekal {b['recall']:.1%} -> {a['recall']:.1%}   "
                  f"presisi {b['precision']:.1%} -> {a['precision']:.1%}   "
                  f"talasemia di antrean {b['chronic_in_queue']:.1%} -> {a['chronic_in_queue']:.1%}")
    if args.json:
        Path(args.json).write_text(json.dumps({"workload": WORKLOAD, "seeds": args.seeds, "results": out}, indent=1))


if __name__ == "__main__":
    main()

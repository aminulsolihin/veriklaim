"""Measure the upcoding check on the synthetic sample, over many random samples.

    python tools/benchmark_coding.py            # 20 samples, as shipped
    python tools/benchmark_coding.py 20 0.5     # half the planted upcoding

The sample is invented and the upcoding is planted by the same author's rules, so these figures
say that the code finds what it was written to find. They are not an estimate for real claims.
"""
import sys, random, statistics
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from app import coding
from make_coding_sample import generate


def one(seed, strength):
    text, labels = generate(seed, strength)
    rows = coding.parse_coding(text)["rows"]
    coding.screen(rows)
    planted = {s for s in labels["claims"]}
    roles = labels["hospitals"]
    comp = [r for r in rows if r["tested"] and r["sev"] >= 2]
    k = coding.default_top_k(len(comp))
    q = coding.queue_positions(rows, k)
    qs = [rows[i] for i in q]
    hit = sum(1 for r in qs if r["sep"] in planted)
    n_pl = sum(1 for r in rows if r["sep"] in planted)
    n_pl_cmp = sum(1 for r in comp if r["sep"] in planted)
    rule = [r for r in rows if r["rule"]]
    rule_hit = sum(1 for r in rule if r["sep"] in planted)
    # baselines over the same number of claims
    rnd = random.Random(seed)
    rand_hit = sum(1 for r in rnd.sample(comp, min(len(qs), len(comp))) if r["sep"] in planted)
    short = sorted(comp, key=lambda r: (r["los"] / max(1, r["typ_los"] or 1), r["pos"]))[: len(qs)]
    short_hit = sum(1 for r in short if r["sep"] in planted)
    # hospital verdicts
    hs = {h["rs"]: h for h in coding.hospital_table([(r["kode_rs"], r["kelas_rs"], r["sev"], r["p_peer"], r["supported"], False) for r in rows if r["tested"]])}
    verdict = {role: [] for role in set(roles.values())}
    for rs, role in roles.items():
        verdict[role].append(hs[rs]["verdict"] if rs in hs else "-")
    live = sorted((r for r in rows if r["live"]), key=lambda r: r["rank"])
    at = {kk: sum(1 for r in live[:kk] if r["sep"] in planted) for kk in (20, 40)}
    return dict(at=at, n=len(rows), comp=len(comp), planted=n_pl, planted_cmp=n_pl_cmp, k=k, queue=len(qs), hit=hit,
                rule=len(rule), rule_hit=rule_hit, rand=rand_hit, short=short_hit, verdict=verdict)


def main():
    runs = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    strength = float(sys.argv[2]) if len(sys.argv) > 2 else 1.0
    res = [one(100 + i, strength) for i in range(runs)]
    def avg(key): return statistics.mean(r[key] for r in res)
    print(f"{runs} samples, strength {strength}: {avg('n'):.0f} claims, {avg('comp'):.0f} coded II/III compared, {avg('planted'):.1f} planted")
    print(f"queue {avg('queue'):.0f} claims (K={avg('k'):.0f}): recall {sum(r['hit'] for r in res)/sum(r['planted'] for r in res):.2f}, precision {sum(r['hit'] for r in res)/sum(r['queue'] for r in res):.2f}")
    for kk in (20, 40):
        print(f"  top {kk} by score:             precision {sum(r['at'][kk] for r in res)/(kk*len(res)):.2f}")
    print(f"  random same size:           precision {sum(r['rand'] for r in res)/sum(r['queue'] for r in res):.2f}")
    print(f"  shortest stay vs level:     precision {sum(r['short'] for r in res)/sum(r['queue'] for r in res):.2f}")
    print(f"  rule only: {avg('rule'):.1f} flags, precision {sum(r['rule_hit'] for r in res)/max(1,sum(r['rule'] for r in res)):.2f}, recall {sum(r['rule_hit'] for r in res)/sum(r['planted'] for r in res):.2f}")
    tally = {}
    for r in res:
        for role, vs in r["verdict"].items():
            for v in vs:
                tally.setdefault(role, {}).setdefault(v, 0)
                tally[role][v] += 1
    for role, d in sorted(tally.items()):
        print(f"  hospital {role:9s}", dict(sorted(d.items())))

main()

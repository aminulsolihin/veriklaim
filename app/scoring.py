"""Features, the anomaly model and the explanations.

Stage 2  bill rule        TARIF_RS divided by the reference price of the top-up item.
Stage 3  anomaly layer    scikit-learn Isolation Forest over four features.
Stage 4  explanations     exact Shapley values (the quantity SHAP estimates). With four
                          features all 16 coalitions are evaluated directly, so the values
                          sum exactly to (score of the claim - average score).
"""
from __future__ import annotations

import math
import re
import statistics
import threading
from collections import OrderedDict, defaultdict
from typing import Callable, Sequence

import numpy as np
from sklearn.ensemble import IsolationForest

from . import baseline

FEATURES = [
    {"key": "cover", "label": "Tagihan RS vs harga acuan"},
    {"key": "los", "label": "Lama rawat vs INA-CBG sama"},
    {"key": "repeat", "label": "Top-up berulang di atas pola diagnosis"},
    {"key": "vol", "label": "Volume top-up RS vs sejawat"},
]
N_FEATURES = len(FEATURES)
N_TREES = 200
MAX_SAMPLES = 256      # subsample per tree, as in Liu, Ting & Zhou (2008)
SEED = 7
BACKGROUND_MAX = 120   # background claims used for the Shapley values
REPEAT_WINDOW_DAYS = 90
EAGER_EXPLAIN = 60     # claims explained at upload; the rest are explained when opened
LOS_PRIOR_CLAIMS = 5   # the national stay median for the diagnosis counts as this many claims of the group

_SEVERITY = re.compile(r"-[IVX0-9]+$")


def featurize(rows: list[dict]) -> np.ndarray:
    """Add the four features to each row and return the model matrix (n x 4).

    Where the claim's primary diagnosis has a national norm (baseline.py), the stay median of its
    INA-CBG group is blended with the national median, and the repeat feature counts only the repeats
    above the number a patient with that diagnosis normally has in the part of the window the file covers."""
    los_by_cbg: dict[str, list[int]] = defaultdict(list)
    count_by_pair: dict[tuple, int] = defaultdict(int)
    by_card: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        r["_cbg"] = _SEVERITY.sub("", r["inacbg"])
        los_by_cbg[r["_cbg"]].append(max(1, r["los"]))
        count_by_pair[(r["kode_rs"], r["cmg_code"])] += 1
        if r["card_token"]:
            by_card[(r["card_token"], r["cmg_code"])].append(r)
    med_los = {k: statistics.median(v) for k, v in los_by_cbg.items()}
    counts_by_code: dict[str, list[int]] = defaultdict(list)
    for (_, code), n in count_by_pair.items():
        counts_by_code[code].append(n)
    med_vol = {k: statistics.median(v) for k, v in counts_by_code.items()}
    first = min((r["adm_day"] for r in rows), default=0)
    last = max((r["adm_day"] for r in rows), default=0)
    w = REPEAT_WINDOW_DAYS

    X = np.empty((len(rows), N_FEATURES), dtype=float)
    for i, r in enumerate(rows):
        r["dx3"] = baseline.primary_dx(r.get("diag", ""))
        nat = baseline.norm(r["dx3"], baseline.is_inpatient(r["inacbg"], r["los"]))
        r["los_nat"] = float(nat["los_p50"]) if nat and nat.get("los_p50") is not None else None
        r["cover"] = r["tarif_rs"] / r["ref_price"]
        n_cbg, med = len(los_by_cbg[r["_cbg"]]), float(med_los[r["_cbg"]])
        if r["los_nat"] is not None:
            med = (n_cbg * med + LOS_PRIOR_CLAIMS * max(1.0, r["los_nat"])) / (n_cbg + LOS_PRIOR_CLAIMS)
        r["los_median"] = med
        r["los_ratio"] = max(1, r["los"]) / max(1.0, r["los_median"])
        same = by_card.get((r["card_token"], r["cmg_code"]), ()) if r["card_token"] else ()
        r["repeat_n"] = sum(1 for o in same if o is not r and abs(o["adm_day"] - r["adm_day"]) <= w)
        # national visits per day for the diagnosis, times the days of the +/- window that this file covers
        seen_days = max(0, min(r["adm_day"] + w, last) - max(r["adm_day"] - w, first))
        r["repeat_exp"] = round(float(nat["near_mean"]) * seen_days / (2 * w), 3) if nat and nat.get("near_mean") else 0.0
        r["vol_n"] = count_by_pair[(r["kode_rs"], r["cmg_code"])]
        r["vol_ratio"] = r["vol_n"] / max(1.0, med_vol[r["cmg_code"]])
        X[i] = feature_vector(r["cover"], r["los_ratio"], r["repeat_n"], r["vol_ratio"], r["repeat_exp"])
        del r["_cbg"]
    return X


def feature_vector(cover: float, los_ratio: float, repeat_n: int, vol_ratio: float, repeat_exp: float = 0.0) -> list[float]:
    return [math.log2(cover), math.log2(los_ratio), max(0.0, repeat_n - (repeat_exp or 0.0)), math.log2(vol_ratio)]


class Model:
    """A fitted forest for one batch. Refitting on the same matrix gives the same forest."""

    def __init__(self, X: np.ndarray) -> None:
        self.X = np.asarray(X, dtype=float)
        n = len(self.X)
        self.forest = IsolationForest(
            n_estimators=N_TREES, max_samples=min(MAX_SAMPLES, n), random_state=SEED
        ).fit(self.X)
        # scikit-learn returns the negated score; this is s(x) in (0, 1] from the paper.
        self.scores = -self.forest.score_samples(self.X)
        step = max(1, n // BACKGROUND_MAX)
        self.background = self.X[::step]
        self.base = float(self.scores[::step].mean())

    def ranks(self) -> np.ndarray:
        """1 = most unusual. Ties keep file order."""
        order = np.argsort(-self.scores, kind="stable")
        ranks = np.empty(len(order), dtype=int)
        ranks[order] = np.arange(1, len(order) + 1)
        return ranks

    def shapley(self, idx: Sequence[int]) -> np.ndarray:
        """Exact Shapley values for the claims at positions `idx` (len(idx) x 4).

        A feature outside a coalition is replaced by each background claim in turn."""
        idx = list(idx)
        m, bg = N_FEATURES, self.background
        n_full = 1 << m
        inner = list(range(1, n_full - 1))
        out = np.zeros((len(idx), m))
        chunk = max(1, 150_000 // (len(inner) * len(bg)))
        weights = [math.factorial(k) * math.factorial(m - k - 1) / math.factorial(m) for k in range(m)]
        for start in range(0, len(idx), chunk):
            sub = idx[start:start + chunk]
            block = np.empty((len(sub), len(inner), len(bg), m))
            block[:] = bg
            for j, mask in enumerate(inner):
                for f in range(m):
                    if mask >> f & 1:
                        block[:, j, :, f] = self.X[sub, f][:, None]
            vals = -self.forest.score_samples(block.reshape(-1, m))
            v = np.empty((len(sub), n_full))
            v[:, 0] = self.base
            v[:, -1] = self.scores[sub]
            v[:, 1:-1] = vals.reshape(len(sub), len(inner), len(bg)).mean(axis=2)
            for i in range(m):
                for mask in range(n_full):
                    if mask >> i & 1:
                        continue
                    out[start:start + len(sub), i] += weights[bin(mask).count("1")] * (v[:, mask | 1 << i] - v[:, mask])
        return out


def screen(rows: list[dict]) -> Model:
    """Featurize, fit, score and rank `rows` in place; explain the most unusual ones."""
    model = Model(featurize(rows))
    ranks = model.ranks()
    for i, r in enumerate(rows):
        r["pos"] = i
        r["score"] = float(model.scores[i])
        r["rank"] = int(ranks[i])
        r["phi"] = None
    eager = [i for i, r in enumerate(rows) if r["rank"] <= EAGER_EXPLAIN]
    for i, phi in zip(eager, model.shapley(eager)):
        rows[i]["phi"] = [float(x) for x in phi]
    return model


def default_top_k(n: int) -> int:
    return min(max_top_k(n), max(4, round(n * 0.15)))


def max_top_k(n: int) -> int:
    return max(4, min(40, n * 3 // 10))


# Fitted models are kept in memory and rebuilt from the stored features when needed
# (after a restart, or when the cache is full).
_cache: "OrderedDict[int, Model]" = OrderedDict()
_cache_lock = threading.Lock()
_CACHE_SIZE = 6


def remember(batch_id: int, model: Model) -> None:
    with _cache_lock:
        _cache[batch_id] = model
        _cache.move_to_end(batch_id)
        while len(_cache) > _CACHE_SIZE:
            _cache.popitem(last=False)


def forget(batch_id: int) -> None:
    with _cache_lock:
        _cache.pop(batch_id, None)


def model_for(batch_id: int, load_matrix: Callable[[], np.ndarray]) -> Model:
    with _cache_lock:
        model = _cache.get(batch_id)
        if model is not None:
            _cache.move_to_end(batch_id)
            return model
    model = Model(load_matrix())
    remember(batch_id, model)
    return model

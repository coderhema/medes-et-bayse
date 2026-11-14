"""Train + evaluate the Bayse prediction SLM on resolved Bayse markets.

    python -m medes_et_bayse.slm.collect --out data/resolved_markets.json   # public GETs only
    python -m medes_et_bayse.slm.train --data data/resolved_markets.json

Protocol (no leakage):
  * Snapshots are taken H hours before each market's resolution (H in 12/24/72/168 for training);
    every feature uses only data at or before the snapshot time.
  * Events are split by resolution date: the oldest 60% train, the newest 40% test. Whole events
    stay on one side. Hyper-parameters are chosen on a further time split inside train
    (last 25% of train events = validation); the test set is scored exactly once.
  * Uncertainty: 1,000-rep bootstrap resampling *events* (multi-candidate events are correlated).
  * Reported test metrics come from the model fitted on train only. The shipped artifact is then
    refit on all data with the same hyper-parameters (pass --deploy-fit train to skip that).
"""
from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

import numpy as np
from sklearn.decomposition import PCA
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression

from ..agent.strategy import choose_side, effective_price
from .collect import load_rows
from .features import MarketSnapshot, NUMERIC_FEATURES, logit, parse_ts, price_at, numeric_features
from .model import DEFAULT_ARTIFACT, ENCODER_NAME, load_encoder

TRAIN_HORIZONS = (12, 24, 72, 168)
EVAL_HORIZONS = {"24h": 24, "7d": 168}
EPS = 1e-3
TEXT_MARGIN = 0.002  # validation log-loss improvement required before the text head becomes the default


def build_snapshots(rows: list[dict], horizons=TRAIN_HORIZONS, min_orders: int = 5) -> list[dict]:
    out = []
    for r in rows:
        res = parse_ts(r.get("resolution_date")) or parse_ts(r.get("closing"))
        hist = sorted(r.get("history") or [], key=lambda pt: pt["e"])
        if not res or not hist or (r.get("market_orders") or 0) < min_orders:
            continue
        created = parse_ts(r.get("created"))
        for h in horizons:
            t = res - timedelta(hours=h)
            if created and t < created:
                continue
            p = price_at(hist, int(t.timestamp() * 1000))
            if p is None or not (0 < p < 1):
                continue
            snap = MarketSnapshot(
                yes_price=float(p), as_of=t, category=r.get("category", ""), event_title=r.get("event_title", ""),
                market_title=r.get("market_title", ""), closing_date=parse_ts(r.get("closing")), created_at=created,
                liquidity=float(r.get("liquidity") or 0), fee_pct=float(r.get("fee_pct") or 10),
                n_markets=int(r.get("n_markets") or 1), history=hist,
            )
            out.append({"snap": snap, "y": int(r["outcome"]), "h": h, "event": r["event_id"], "res": res,
                        "fee": float(r.get("fee_pct") or 10), "market": r["market_id"]})
    return out


def metrics(q, y):
    q = np.clip(np.asarray(q, float), EPS, 1 - EPS); y = np.asarray(y, float)
    return {"brier": float(np.mean((q - y) ** 2)),
            "logloss": float(-np.mean(y * np.log(q) + (1 - y) * np.log(1 - q))),
            "hit_rate": float(np.mean((q >= 0.5) == (y == 1)))}


def calibration(q, y, edges=(0, .1, .2, .3, .4, .5, .6, .7, .8, .9, 1.0001)):
    q = np.asarray(q); y = np.asarray(y); rows = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (q >= lo) & (q < hi)
        if m.any():
            rows.append({"bin": f"{lo:.1f}-{min(hi, 1):.1f}", "n": int(m.sum()), "mean_pred": round(float(q[m].mean()), 3), "freq_yes": round(float(y[m].mean()), 3)})
    return rows


def event_bootstrap(stat_fn, groups, n=1000, seed=7):
    """stat_fn(indices) over resampled events -> (mean, lo95, hi95)."""
    rng = np.random.default_rng(seed)
    by_event = defaultdict(list)
    for i, g in enumerate(groups):
        by_event[g].append(i)
    keys = list(by_event)
    vals = []
    for _ in range(n):
        idx = [i for k in rng.choice(len(keys), len(keys)) for i in by_event[keys[k]]]
        v = stat_fn(np.asarray(idx))
        if v is not None and not math.isnan(v):
            vals.append(v)
    if not vals:
        return None
    return {"mean": float(np.mean(vals)), "lo95": float(np.percentile(vals, 2.5)), "hi95": float(np.percentile(vals, 97.5))}


def backtest(q, p, y, fees, groups, min_edge):
    """Flat $1 BUY on the side whose post-fee edge >= min_edge. Returns ROI per $1 staked."""
    pnl, grp = [], []
    for qi, pi, yi, fi, gi in zip(q, p, y, fees, groups):
        side = choose_side(qi, pi, 1 - pi, fee_pct=fi, min_edge=min_edge)
        if side is None:
            continue
        outcome, price, eff, _, _ = side
        won = (yi == 1) == (outcome == "YES")
        pnl.append((1 / eff - 1) if won else -1.0); grp.append(gi)
    res = {"min_edge": min_edge, "n_bets": len(pnl), "n_events": len(set(grp))}
    if pnl:
        arr = np.asarray(pnl)
        res["roi_per_$1"] = round(float(arr.mean()), 4)
        res["hit_rate"] = round(float(np.mean(arr > 0)), 4)
        ci = event_bootstrap(lambda idx: float(arr[idx].mean()) if len(idx) else None, grp)
        if ci:
            res["roi_ci95"] = [round(ci["lo95"], 4), round(ci["hi95"], 4)]
    return res


class Featurizer:
    def __init__(self, encoder=None, k: int = 0):
        self.encoder, self.k, self.pca, self._cache = encoder, k, None, {}

    def _emb(self, texts):
        missing = [t for t in set(texts) if t not in self._cache]
        if missing:
            for t, e in zip(missing, self.encoder.encode(missing)):
                self._cache[t] = np.asarray(e, float)
        return np.vstack([self._cache[t] for t in texts])

    def fit(self, items):
        if self.k and self.encoder is not None:
            texts = sorted({it["snap"].text for it in items})
            self.pca = PCA(n_components=self.k, random_state=0).fit(self._emb(texts))
        return self

    def transform(self, items):
        X = np.asarray([numeric_features(it["snap"]) for it in items], float)
        if self.pca is not None:
            X = np.hstack([X, self.pca.transform(self._emb([it["snap"].text for it in items]))])
        return X


def weights(items):
    count = defaultdict(int)
    for it in items:
        count[(it["event"], it["h"])] += 1
    return np.asarray([1.0 / count[(it["event"], it["h"])] for it in items])


def fit_lr(X, y, w, C):
    mu = X.mean(0); sd = X.std(0); sd[sd < 1e-9] = 1.0
    lr = LogisticRegression(C=C, max_iter=5000).fit((X - mu) / sd, y, sample_weight=w)
    return lr, mu, sd


def predict_lr(model, X):
    lr, mu, sd = model
    return lr.predict_proba((X - mu) / sd)[:, 1]


def head_json(model, fz: Featurizer):
    lr, mu, sd = model
    head = {"coef": lr.coef_[0].tolist(), "intercept": float(lr.intercept_[0]), "mu": mu.tolist(), "sigma": sd.tolist(),
            "features": list(NUMERIC_FEATURES) + [f"text_pc{i}" for i in range(fz.k if fz.pca is not None else 0)]}
    if fz.pca is not None:
        head["pca_mean"] = fz.pca.mean_.tolist(); head["pca_components"] = fz.pca.components_.tolist()
    return head


def split_by_event_time(items, frac):
    ev_time = {}
    for it in items:
        ev_time[it["event"]] = max(ev_time.get(it["event"], it["res"]), it["res"])
    order = sorted(ev_time, key=ev_time.get)
    cut = set(order[: int(len(order) * frac)])
    return [it for it in items if it["event"] in cut], [it for it in items if it["event"] not in cut], ev_time[order[int(len(order) * frac)]]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data/resolved_markets.json.gz")
    ap.add_argument("--out", default=str(DEFAULT_ARTIFACT))
    ap.add_argument("--metrics-out", default=str(Path(DEFAULT_ARTIFACT).with_name("metrics.json")))
    ap.add_argument("--no-text", action="store_true")
    ap.add_argument("--deploy-fit", choices=["all", "train"], default="all")
    args = ap.parse_args(argv)

    rows = load_rows(args.data)
    items = build_snapshots(rows)
    train, test, test_start = split_by_event_time(items, 0.6)
    inner, val, _ = split_by_event_time(train, 0.75)
    encoder = None if args.no_text else load_encoder(ENCODER_NAME)
    print(f"snapshots={len(items)} train={len(train)} test={len(test)} test_starts={test_start.date()}")

    y_of = lambda its: np.asarray([it["y"] for it in its])
    # ---- model selection on validation (pooled horizons) ----
    candidates = []
    for k in ([0] if encoder is None else [0, 8, 16, 32]):
        fz = Featurizer(encoder, k).fit(inner)
        Xi, Xv = fz.transform(inner), fz.transform(val)
        for C in (0.03, 0.1, 0.3, 1.0):
            m = fit_lr(Xi, y_of(inner), weights(inner), C)
            candidates.append({"kind": "lr", "k": k, "C": C, "val": metrics(predict_lr(m, Xv), y_of(val))})
        gb = HistGradientBoostingClassifier(max_depth=3, learning_rate=0.05, max_iter=200, l2_regularization=1.0, random_state=0)
        gb.fit(Xi, y_of(inner), sample_weight=weights(inner))
        candidates.append({"kind": "hgb", "k": k, "val": metrics(gb.predict_proba(Xv)[:, 1], y_of(val))})
    pv = np.asarray([it["snap"].yes_price for it in val])
    val_market = metrics(pv, y_of(val))
    rec = fit_lr(np.asarray([[logit(it["snap"].yes_price)] for it in inner]), y_of(inner), weights(inner), 1.0)
    val_recal = metrics(predict_lr(rec, np.asarray([[logit(p)] for p in pv])), y_of(val))
    best_num = min((c for c in candidates if c["kind"] == "lr" and c["k"] == 0), key=lambda c: c["val"]["logloss"])
    text_cands = [c for c in candidates if c["kind"] == "lr" and c["k"] > 0]
    best_text = min(text_cands, key=lambda c: c["val"]["logloss"]) if text_cands else None
    # Parsimony: the text encoder must beat the text-free head on validation by a real margin.
    use_text = best_text is not None and best_text["val"]["logloss"] < best_num["val"]["logloss"] - TEXT_MARGIN
    print("val market", val_market, "recal", val_recal)
    for c in candidates:
        print(" cand", c)

    # ---- fit chosen configs on full train, score test once ----
    def fit_cfg(cfg, its):
        fz = Featurizer(encoder if cfg["k"] else None, cfg["k"]).fit(its)
        return fz, fit_lr(fz.transform(its), y_of(its), weights(its), cfg["C"])

    fz_num, m_num = fit_cfg(best_num, train)
    fz_txt, m_txt = fit_cfg(best_text, train) if best_text else (None, None)
    rec_full = fit_lr(np.asarray([[logit(it["snap"].yes_price)] for it in train]), y_of(train), weights(train), 1.0)

    report = {"data_rows": len(rows), "snapshots": len(items), "train_snapshots": len(train), "test_snapshots": len(test),
              "test_starts": str(test_start.date()), "train_horizons_h": list(TRAIN_HORIZONS), "selected": {
                  "numeric": best_num, "text": best_text, "text_used": use_text}, "validation": {
                  "market": val_market, "recalibrated_market": val_recal, "candidates": candidates}, "test": {}}
    shipped = ("text" if use_text else "numeric")
    for name, h in EVAL_HORIZONS.items():
        its = [it for it in test if it["h"] == h]
        if not its:
            continue
        y = y_of(its); p = np.asarray([it["snap"].yes_price for it in its]); g = [it["event"] for it in its]
        fees = [it["fee"] for it in its]
        preds = {"market": p, "recalibrated_market": predict_lr(rec_full, np.asarray([[logit(x)] for x in p])),
                 "slm_numeric": predict_lr(m_num, fz_num.transform(its))}
        if best_text:
            preds["slm_text"] = predict_lr(m_txt, fz_txt.transform(its))
        preds["slm"] = preds["slm_text" if use_text else "slm_numeric"]
        sec = {"n_markets": len(its), "n_events": len(set(g)), "yes_rate": round(float(y.mean()), 3)}
        for k, q in preds.items():
            mm = metrics(q, y)
            entry = {kk: round(v, 4) for kk, v in mm.items()}
            if k != "market":
                for stat in ("brier", "logloss"):
                    def diff(idx, q=q, stat=stat):
                        return metrics(q[idx], y[idx])[stat] - metrics(p[idx], y[idx])[stat]
                    ci = event_bootstrap(diff, g)
                    entry[f"{stat}_minus_market_ci95"] = [round(ci["lo95"], 4), round(ci["hi95"], 4)]
                entry["backtest"] = [backtest(q, p, y, fees, g, me) for me in (0.08, 0.05, 0.03)]
            sec[k] = entry
        sec["calibration_market"] = calibration(p, y)
        sec["calibration_slm"] = calibration(preds["slm"], y)
        report["test"][name] = sec

    # ---- deployable artifact ----
    fit_items = items if args.deploy_fit == "all" else train
    fzn, mn = fit_cfg(best_num, fit_items)
    artifact = {"version": "bayse-slm-v1", "encoder": ENCODER_NAME, "trained_on": args.deploy_fit,
                "n_snapshots": len(fit_items), "numeric_head": head_json(mn, fzn), "text_head": None,
                "shipped_head": shipped}
    if best_text:
        fzt, mt = fit_cfg(best_text, fit_items)
        artifact["text_head"] = head_json(mt, fzt)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    json.dump(artifact, open(args.out, "w"))
    json.dump(report, open(args.metrics_out, "w"), indent=1, default=str)
    print(json.dumps(report["test"], indent=1, default=str))
    print("wrote", args.out, Path(args.out).stat().st_size, "bytes")


if __name__ == "__main__":
    main()

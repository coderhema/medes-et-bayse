"""Collect resolved Bayse binary markets + public YES price history (read-only, no auth).

    python -m medes_et_bayse.slm.collect --out data/resolved_markets.json.gz \
        --pages politics:1-10 entertainment:1-32 sports:1-75 economy:1-4 culture:1-4

Existing rows in --out are kept; new markets are merged in (dedup by market id).
Only GET /v1/pm/events and GET /v1/pm/events/{id}/price-history are called.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import time
import urllib.parse
import urllib.request

BASE = os.getenv("BAYSE_API_URL", "https://relay.bayse.markets").rstrip("/")


def get(path: str, **params):
    err = None
    for attempt in range(4):
        try:
            url = BASE + path + "?" + urllib.parse.urlencode(params, doseq=True)
            req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "medes-et-bayse-collector"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except Exception as exc:  # pragma: no cover - network
            err = exc
            time.sleep(2 * (attempt + 1))
    raise err


def load_rows(path):
    """Load a dataset written by save_rows (gzip or plain JSON, detected by content)."""
    if not os.path.exists(path):
        return []
    with open(path, "rb") as fh:
        raw = fh.read()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return json.loads(raw.decode("utf-8"))


def save_rows(path, rows):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    data = json.dumps(rows, separators=(",", ":")).encode("utf-8")
    if str(path).endswith(".gz"):
        data = gzip.compress(data, mtime=0)
    tmp = str(path) + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


def event_rows(event: dict, category: str) -> list[dict]:
    markets = [m for m in event.get("markets", []) if m.get("resolvedOutcome") in ("YES", "NO")]
    if not markets or not (event.get("totalOrders") or 0):
        return []
    hist = get(f"/v1/pm/events/{event['id']}/price-history", timePeriod="1Y", outcome="YES")
    by_market = {m["marketId"]: m.get("priceHistory") or [] for m in hist.get("markets", [])}
    return [{
        "category": category, "event_id": event["id"], "event_title": event.get("title", ""), "market_id": m["id"],
        "market_title": m.get("title", ""), "engine": event.get("engine"), "type": event.get("type"),
        "n_markets": len(event.get("markets", [])), "fee_pct": m.get("feePercentage"),
        "event_orders": event.get("totalOrders"), "market_orders": m.get("totalOrders"),
        "liquidity": event.get("liquidity"), "volume": event.get("totalVolume"),
        "created": event.get("createdAt"), "closing": event.get("closingDate"),
        "resolution_date": event.get("resolutionDate"), "resolved_at": event.get("resolvedAt"),
        "outcome": 1 if m["resolvedOutcome"] == "YES" else 0, "history": by_market.get(m["id"], []),
    } for m in markets]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/resolved_markets.json.gz")
    ap.add_argument("--pages", nargs="+", default=["politics:1-10", "entertainment:1-32", "economy:1-4", "culture:1-4", "sports:1-75"])
    ap.add_argument("--sleep", type=float, default=0.15)
    args = ap.parse_args(argv)
    rows = load_rows(args.out)
    seen = {r["market_id"] for r in rows}
    seen_events = {r["event_id"] for r in rows}
    for spec in args.pages:
        cat, rng = spec.split(":")
        lo, hi = (int(x) for x in rng.split("-"))
        for page in range(lo, hi + 1):
            evs = get("/v1/pm/events", status="resolved", category=cat, page=page, size=20).get("events", [])
            if not evs:
                break
            for ev in evs:
                if ev["id"] in seen_events:
                    continue
                try:
                    new = [r for r in event_rows(ev, cat) if r["market_id"] not in seen]
                except Exception as exc:  # pragma: no cover
                    print("skip", ev.get("id"), exc, file=sys.stderr)
                    continue
                rows.extend(new); seen.update(r["market_id"] for r in new); seen_events.add(ev["id"])
                time.sleep(args.sleep)
            save_rows(args.out, rows)
            print(f"{cat} page {page}: total rows {len(rows)}", file=sys.stderr, flush=True)
    print(len(rows))


if __name__ == "__main__":
    main()

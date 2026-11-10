"""Read-only end-to-end run of the agent loop against LIVE public Bayse data.

* No credentials are used (market data endpoints are public).
* BayseClient.place_order is replaced with a function that raises, and DRY_RUN is forced on,
  so no order can be sent even by mistake.
* Telegram is not contacted: the cycle report is captured locally.

    python harness/run_live.py            # writes harness/output/live_run.json
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["DRY_RUN"] = "true"
for key in ("TELEGRAM_BOT_TOKEN", "BOT_TOKEN", "BAYSE_PUBLIC_KEY", "BAYSE_SECRET_KEY"):
    os.environ.pop(key, None)

from medes_et_bayse.agent import AgentConfig, TradingAgent  # noqa: E402
from medes_et_bayse.agent.store import AgentStore  # noqa: E402
from medes_et_bayse.client import BayseClient  # noqa: E402


def _blocked(*args, **kwargs):
    raise RuntimeError("place_order is blocked in the read-only harness")


BayseClient.place_order = _blocked  # type: ignore[assignment]


def main() -> None:
    cfg = AgentConfig.from_env()
    assert cfg.dry_run
    client = BayseClient(api_key="", api_secret="")
    reports: list[str] = []
    db = Path(tempfile.mkdtemp()) / "harness.db"
    agent = TradingAgent(client, cfg, store=AgentStore(db), reporter=reports.append)
    report = agent.cycle()
    preds = [dict(r) for r in agent.store._exec(
        "SELECT title, category, yes_price, p_slm, p_yes, hours_to_close, decision, reason FROM predictions ORDER BY ABS(p_yes - yes_price) DESC LIMIT 10").fetchall()]
    out = {
        "slm_version": agent.predictor.slm.version,
        "slm_uses_text": agent.predictor.slm.uses_text,
        "config": cfg.__dict__,
        "scanned": report.scanned,
        "tradeable": report.tradeable,
        "skipped": report.skipped,
        "shortlisted": report.shortlisted,
        "top_candidates": report.top_candidates,
        "trades": report.trades,
        "errors": report.errors,
        "largest_model_vs_market_gaps": preds,
        "telegram_report_captured": reports,
    }
    path = ROOT / "harness" / "output" / "live_run.json"
    path.write_text(json.dumps(out, indent=1, default=str))
    print(json.dumps(out, indent=1, default=str))


if __name__ == "__main__":
    main()

<img width="124" height="124" alt="share_1774484367512" src="https://github.com/user-attachments/assets/fe9107ec-5177-4ddc-806d-1a718eb9bfae" />

# medes-et-bayse

An autonomous trading agent for [Bayse Markets](https://bayse.markets), a prediction market platform based in Africa. It uses a small, CPU-only **prediction SLM** trained on resolved Bayse markets, plus a **fee-aware** edge filter, fractional-Kelly sizing and hard risk limits.

> **Not financial advice. Dry run by default.** `DRY_RUN=true` is the default: the agent logs what it *would* buy and sends no orders. Read the metrics below before you think about turning it off. The model does **not** reliably beat the market after fees.

## Architecture (v2)

```
                    ┌──────────────── TradingAgent.cycle() (medes_et_bayse/agent/loop.py) ────────────────┐
 Bayse public API → │ SETTLE past trades → SCAN open events → FILTER (orders, liquidity, 6h–7d to close,  │
 (no auth needed)   │ modelled categories, price history) → PREDICT (SLM) → RESEARCH shortlist (optional │
                    │ Tavily + LLM blend) → EDGE after fees (≥ 0.08) → SIZE (¼–½ Kelly, ≤5%/trade) →      │
                    │ RISK (daily loss budget, open positions, 1 position/market) → EXECUTE (BUY only;    │
                    │ dry-run unless DRY_RUN=false) → LOG (SQLite) → REPORT (Telegram)                   │
                    └─────────────────────────────────────────────────────────────────────────────────────┘
```

| Module | Role |
|---|---|
| `main.py` | Entry point (`Procfile: worker: python main.py`). Runs the health server, the agent thread and the optional Telegram bot (`/agent`, `/quote`, `/order`, `/events`, `/balance`, `/portfolio`, `/help`) |
| `medes_et_bayse/agent/markets.py` | Parses `/v1/pm/events` into per-market views: `outcome1Price`/`outcome2Price` (a `0` buy price counts as missing), event/market/outcome UUIDs, fee, liquidity, orders, closing date |
| `medes_et_bayse/agent/predictor.py` | SLM probability. Optionally blends an LLM view (logit space, weight `LLM_BLEND_WEIGHT`) when `TAVILY_API_KEY` + `LLM_API_KEY`/`LLM_MODEL` are set |
| `medes_et_bayse/agent/strategy.py` | Bayse fee `feeRate·max(1−P, 0.5)`, post-fee edge, **BUY YES or BUY NO** (never sells shares it doesn't hold), fractional Kelly |
| `medes_et_bayse/agent/risk.py` | Market filters. Daily loss budget (today's stakes count as fully at risk, minus realized PnL), max open positions, one position per market |
| `medes_et_bayse/agent/store.py` | SQLite: `cycles`, `predictions` (the live track record, with `resolved_outcome` filled in on settlement), `trades`, `notes` |
| `medes_et_bayse/slm/` | Prediction SLM: features, runtime, training, data collector |
| `medes_et_bayse/client.py` | Bayse REST client. Orders use the documented body `{side, outcomeId, amount, type, currency[, price]}` |
| `harness/run_live.py` | Read-only end-to-end run against live public data with `place_order` blocked |

Mutually exclusive multi-market events (e.g. elections, where the YES prices sum to about 1) are renormalised so the modelled probabilities stay coherent.

The v1 code (the Hermes/Groq loop, the PyFlue/"Ralph" bot in `bot/`, the corrupted `bot/main.py`, and the "arbitrage" strategy, which never fired because AMM Yes+No is always 1) has been removed.

## The prediction SLM

**Design:** `potion-base-8M` ([model2vec](https://github.com/MinishLab/model2vec)), a ~30 MB static-embedding model distilled from a MiniLM-class sentence encoder, embeds the market question on CPU with no torch. Its PCA components are joined with leakage-safe market features: logit price, extremity, 12h/72h momentum, drift since open, number of price changes, hours to close, market age, liquidity, fee, multi-market flag and category. An L2-regularised logistic head turns these into a calibrated P(YES). Logistic regression is calibrated by construction and fits the ~9k snapshots available. Gradient boosting was also tried and did no better on validation.

**Shipped head:** a time-based validation split decides the default. The text head did not beat the text-free head on validation by the required margin (0.002 log loss), so the shipped default is the **text-free head**. The text head is also in the artifact (`SLM_USE_TEXT=true`) and is reported below. The artifact is `medes_et_bayse/slm/artifacts/bayse_slm.json` (~100 KB). The encoder downloads from Hugging Face on first use. If it can't be loaded, the text-free head is used, so the SLM always works standalone.

**Data:** 6,556 resolved binary markets (politics, sports, entertainment, economy, culture) with their public YES price history, in `data/resolved_markets.json.gz` (collected with public GETs only).

**Protocol:** snapshots are taken 12/24/72/168 h before resolution, and every feature uses only data at or before the snapshot. Events are split by resolution date: the oldest 60% train, the newest 40% test (test starts 2026-05-23). Whole events stay on one side. Hyper-parameters are chosen on the last 25% of train. Uncertainty is a 1,000-rep bootstrap that resamples **events**. Test metrics come from the train-only fit. The shipped artifact is refit on all data with the same settings.

### Test-set results (newest markets, never seen in training)

24h: 1322 markets / 617 events. 7d: 198 markets / 129 events. Lower Brier and log loss are better; the CI columns show *model minus market* (negative = model better).

| Horizon | Model | Brier | Log loss | Hit rate | Brier Δ vs market, 95% CI | Log loss Δ vs market, 95% CI |
|---|---|---|---|---|---|---|
| 24h | Market price (baseline) | 0.1706 | 0.5073 | 74.1% | – | – |
| 24h | Recalibrated market price | 0.1703 | 0.5039 | 74.2% | [-0.0009, +0.0002] | [-0.0051, -0.0018] |
| 24h | **SLM (shipped, text-free head)** | 0.1690 | 0.4998 | 73.7% | [-0.0041, +0.0008] | [-0.0145, +0.0009] |
| 24h | SLM with MiniLM-class text head | 0.1737 | 0.5105 | 73.2% | [-0.0001, +0.0061] | [-0.0055, +0.0125] |
| 7d | Market price (baseline) | 0.1044 | 0.3293 | 84.9% | – | – |
| 7d | Recalibrated market price | 0.1032 | 0.3220 | 84.3% | [-0.0030, +0.0007] | [-0.0123, -0.0015] |
| 7d | **SLM (shipped, text-free head)** | 0.1041 | 0.3202 | 83.8% | [-0.0086, +0.0085] | [-0.0320, +0.0159] |
| 7d | SLM with MiniLM-class text head | 0.1033 | 0.3198 | 83.8% | [-0.0106, +0.0083] | [-0.0364, +0.0190] |

### Fee-aware backtest (flat $1 BUY when post-fee edge ≥ threshold, Bayse fees included)

| Horizon | Model | Min edge | Bets | Hit | ROI per $1 | 95% CI (event bootstrap) |
|---|---|---|---|---|---|---|
| 24h | SLM shipped | 0.08 | 85 (78 events) | 73% | +11.4% | [-1.7%, +24.7%] |
| 24h | SLM shipped | 0.05 | 168 (154 events) | 76% | +13.7% | [+4.3%, +24.0%] |
| 24h | SLM shipped | 0.03 | 263 (223 events) | 72% | +5.0% | [-4.0%, +14.2%] |
| 24h | SLM text head | 0.08 | 174 (158 events) | 56% | -7.1% | [-19.7%, +5.8%] |
| 24h | SLM text head | 0.05 | 336 (281 events) | 57% | -6.7% | [-15.3%, +1.3%] |
| 24h | SLM text head | 0.03 | 514 (402 events) | 57% | -5.4% | [-12.2%, +1.9%] |
| 7d | SLM shipped | 0.08 | 40 (36 events) | 72% | +10.6% | [-6.9%, +29.0%] |
| 7d | SLM shipped | 0.05 | 60 (54 events) | 68% | +2.1% | [-14.5%, +17.4%] |
| 7d | SLM shipped | 0.03 | 86 (72 events) | 64% | -7.6% | [-23.1%, +8.5%] |
| 7d | SLM text head | 0.08 | 42 (37 events) | 79% | +21.3% | [+3.9%, +37.8%] |
| 7d | SLM text head | 0.05 | 62 (57 events) | 73% | +7.0% | [-9.0%, +22.0%] |
| 7d | SLM text head | 0.03 | 85 (74 events) | 68% | +0.2% | [-14.6%, +14.3%] |

**Honest read:**
- The SLM is **about as accurate as the market price**. The 24h log-loss improvement is small and its 95% CI touches zero. At 7 days it is a statistical tie.
- At the production threshold (0.08) the backtest ROI is positive at both horizons, but **the CIs include zero** and the bet counts are small, so this is *not* evidence of an edge after fees.
- The text head looked slightly better on validation but was worse at 24h on test, and the two heads disagree in sign at 7 days. Many thresholds × heads × horizons were compared, so any single "good" cell is likely noise.
- The parsimony margin for the text head was added after a first test run. The shipped-head test numbers are therefore slightly optimistic.
- Snapshot times are measured from Bayse's settlement time, which can come after the outcome was known. That can flatter both the market and the model.
- Treat this as a research baseline. Keep `DRY_RUN=true` and use the `predictions` table to build a live, out-of-sample track record.

### Retrain

```bash
pip install -r requirements-dev.txt
# optional: refresh/extend the dataset (public GETs only)
python -m medes_et_bayse.slm.collect --out data/resolved_markets.json.gz --pages politics:1-10 entertainment:1-32 sports:1-75 economy:1-4 culture:1-4
python -m medes_et_bayse.slm.train --data data/resolved_markets.json.gz      # writes artifacts/bayse_slm.json + metrics.json
python -m medes_et_bayse.slm.train --no-text                                 # text-free only (no encoder download)
```

## Setup and run

```bash
pip install -r requirements.txt
cp .env.example .env        # fill in what you need; nothing is required for a dry run
python main.py              # agent loop (dry run) + Telegram bot if TELEGRAM_BOT_TOKEN is set
python harness/run_live.py  # one read-only cycle on live data, orders blocked -> harness/output/live_run.json
pytest -q
```

Key settings (see `.env.example`):

| Setting | Default | Notes |
|---|---|---|
| `DRY_RUN` | `true` | |
| `MIN_EDGE` | 0.08 | Post-fee edge; lower values are raised to 0.08 |
| `KELLY_FRACTION` | 0.25 | Clamped to 0.25–0.5 |
| `MAX_POSITION_FRACTION` | 0.05 | |
| `MAX_DAILY_LOSS_FRACTION` | 0.10 | |
| `MIN_ORDERS` | 10 | |
| `MIN_HOURS_TO_CLOSE` / `MAX_DAYS_TO_CLOSE` | 6 / 7 | The SLM was trained on markets ≤ ~7 days from close |

Live trading needs `BAYSE_PUBLIC_KEY` and `BAYSE_SECRET_KEY`. Account details (`BAYSE_USER_ID`, `BAYSE_EMAIL`, `TELEGRAM_CHAT_ID`) come only from env.

Deploy as a worker (Railway/Render): the start command is `python main.py`, with `DATABASE_PATH` on a persistent disk.

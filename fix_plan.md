# fix_plan.md — medes-et-bayse Migration & Cycle Tasks

## Migration: Poke → PyFlue + Ralph Loop

### Phase 1: Remove Poke [DONE]
- [x] Delete `bot/poke_client.py`
- [x] Remove Poke imports from `bot/main.py`
- [x] Remove POKE_API_KEY, POKE_WEBHOOK_URL from `.env.example`
- [x] Remove `poke` from `requirements.txt`

### Phase 2: Install New Dependencies [DONE]
- [x] Add `pyflue` to requirements.txt
- [x] Add `tavily-python` to requirements.txt
- [x] Add `aiosqlite` to requirements.txt
- [x] `python-telegram-bot` already present (>=21.0)

### Phase 3: PyFlue Agent [DONE]
- [x] Create `bot/agent.py` — PyFlue agent entry point with Ralph loop
- [x] Create `bot/tools/__init__.py`
- [x] Create `bot/tools/search.py` — Tavily web search tool
- [x] Create `bot/tools/memory.py` — SQLite read/write tool
- [x] Create `bot/tools/bayse.py` — Bayse API wrapper tool

### Phase 4: Ralph Loop Pattern [DONE]
- [x] Implement Goal → Plan → Execute → Reflect → Repeat cycle
- [x] Scan markets → Research with Tavily → Evaluate signals → Execute trades → Log + Report
- [x] Push cycle summaries to Telegram after each cycle
- [x] Support /goal to start, /stop to halt

### Phase 5: SQLite Memory Schema [DONE]
- [x] Create `trades` table (market_id, side, price, amount, edge, strategy, status, etc.)
- [x] Create `market_research` table (query, source, summary, market_id, etc.)
- [x] Create `agent_notes` table (cycle_id, category, note, metadata_json, etc.)
- [x] Persist to `DATABASE_PATH` (default: `/data/medes.db`)

### Phase 6: Telegram Interface [DONE]
- [x] `/goal` — Start autonomous Ralph loop
- [x] `/stop` — Halt the loop
- [x] `/status` — Show current agent state + recent trades + notes
- [x] `/trades` — Show recent trade log

### Phase 7: Deployment [DONE]
- [x] Create `render.yaml` — Background Worker config
- [x] Update `requirements.txt` — add new deps, remove Poke deps
- [x] Update `.env.example` — remove Poke vars, add Tavily/DATABASE_PATH
- [x] Start command: `python bot/agent.py`

### Phase 8: Documentation [DONE]
- [x] Create `PROMPT.md` — Agent goal and Ralph loop definition
- [x] Create `fix_plan.md` — This file

## Cycle Tasks (Ongoing)

Each Ralph loop cycle performs:
1. **Scan** — Fetch 30 open markets from Bayse
2. **Signal** — Run Kelly + Arbitrage strategies
3. **Research** — Tavily search for top 3 signals
4. **Execute** — Place trades (dry-run or live)
5. **Log** — Write trades, research, and notes to SQLite
6. **Report** — Push cycle summary to Telegram
7. **Wait** — Sleep until next cycle interval (default 300s)

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `BAYSE_PUBLIC_KEY` | Yes | — | Bayse Markets public key |
| `BAYSE_SECRET_KEY` | Yes | — | Bayse Markets secret key |
| `TAVILY_API_KEY` | Yes | — | Tavily Search API key |
| `TELEGRAM_BOT_TOKEN` | Yes | — | Telegram bot token |
| `DATABASE_PATH` | No | `/data/medes.db` | SQLite database path |
| `DRY_RUN` | No | `true` | Simulate trades (true/false) |
| `BANKROLL` | No | `100.0` | Total capital in USDC |
| `MIN_EDGE` | No | `0.03` | Minimum edge to trade |
| `MAX_POSITION_FRACTION` | No | `0.05` | Max position per trade |
| `CYCLE_INTERVAL_SECONDS` | No | `300` | Seconds between cycles |
| `TELEGRAM_CHAT_ID` | No | `6433282551` | Chat ID for notifications |

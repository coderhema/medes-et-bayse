<img width="124" height="124" alt="share_1774484367512" src="https://github.com/user-attachments/assets/fe9107ec-5177-4ddc-806d-1a718eb9bfae" />

# medes-et-bayse

An autonomous trading agent for [Bayse Markets](https://bayse.markets) — Africa's largest prediction market platform. Uses **PyFlue** for agent orchestration with a **Ralph loop** pattern (Goal → Plan → Execute → Reflect → Repeat).

## Architecture

```
User sends /goal in Telegram
       │
       ▼
  ┌─────────────────┐
  │   Ralph Loop     │ ← PyFlue agent entry point (bot/agent.py)
  │  Goal→Plan→     │
  │  Execute→Reflect │
  │  →Repeat         │
  └────────┬────────┘
           │
    ┌──────┼──────┐────────┐
    ▼      ▼      ▼        ▼
 Tavily  Bayse   Kelly   SQLite
 Search  Markets Criterion Memory
 (research) (scan) (signals) (log)
```

## Strategies Implemented

- **Kelly Criterion** — Size positions optimally based on edge and bankroll
- **Arbitrage Detection** — Spot mispriced markets where Yes + No < 1 (implied probability gap)

## Setup

```bash
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill in your credentials:

```bash
cp .env.example .env
```

### Environment Variables

| Variable | Description |
|---|---|
| `BAYSE_PUBLIC_KEY` | Bayse Markets public key |
| `BAYSE_SECRET_KEY` | Bayse Markets secret key (HMAC-SHA256) |
| `TAVILY_API_KEY` | Tavily Search API key (for market research) |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token from @BotFather |
| `DATABASE_PATH` | SQLite database path (default: `/data/medes.db`) |
| `DRY_RUN` | Set to `true` to simulate trades (default: `true`) |
| `BANKROLL` | Total capital allocated to the bot (USDC) |
| `MAX_POSITION_FRACTION` | Max fraction of bankroll per trade (default: 0.05) |
| `MIN_EDGE` | Minimum edge threshold before trading (default: 0.03) |
| `CYCLE_INTERVAL_SECONDS` | Seconds between Ralph loop cycles (default: 300) |

## Running the Agent

```bash
# Start the PyFlue agent with Telegram bot
python bot/agent.py
```

### Telegram Commands

| Command | Description |
|---|---|
| `/goal` | Start the autonomous Ralph loop |
| `/stop` | Stop the active loop |
| `/status` | Show agent status, recent trades, and notes |
| `/trades` | Show recent trade log |

## Ralph Loop Cycle

Each cycle:
1. **Scan** — Fetch open markets from Bayse API
2. **Research** — Use Tavily to gather news context for high-signal markets
3. **Evaluate** — Run Kelly Criterion + Arbitrage strategies
4. **Execute** — Place trades (or simulate in dry-run mode)
5. **Log** — Write everything to SQLite (trades, research, reasoning)
6. **Report** — Push cycle summary to Telegram chat

## SQLite Memory Schema

Three tables at `DATABASE_PATH`:

- **trades** — Every trade with market_id, side, price, amount, edge, strategy, status
- **market_research** — Tavily research results tied to markets
- **agent_notes** — Agent reasoning and cycle summaries

## Deployment (Render)

```bash
# Deploy as a Background Worker on Render
# render.yaml is pre-configured
render deploy
```

Requires a persistent disk mounted at `/data` for SQLite storage.

## Project Structure

```
medes-et-bayse/
├── bot/
│   ├── agent.py          # PyFlue agent + Ralph loop + Telegram commands
│   ├── bayse_client.py   # Bayse Markets REST API client
│   ├── main.py           # Legacy entry point (strategies + polling)
│   ├── realtime_feed.py  # WebSocket/REST quote feed
│   ├── strategies/
│   │   ├── arbitrage.py  # Arbitrage detection
│   │   └── kelly.py      # Kelly Criterion sizing
│   ├── telegram_handler.py
│   └── tools/
│       ├── bayse.py      # Bayse API tool for PyFlue
│       ├── memory.py     # SQLite memory tool for PyFlue
│       └── search.py     # Tavily web search tool for PyFlue
├── medes_et_bayse/       # Package (client, auth, config, hermes loop)
├── PROMPT.md             # Agent goal definition
├── fix_plan.md           # Migration & cycle task plan
├── render.yaml           # Render deployment config
├── requirements.txt
└── .env.example
```

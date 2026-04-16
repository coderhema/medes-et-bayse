<img width="124" height="1024" alt="share_1774484367512" src="https://github.com/user-attachments/assets/fe9107ec-5177-4ddc-806d-1a718eb9bfae" />


# medes-et-bayse

A quantitative trading bot for [Bayse Markets](https://bayse.markets) — Africa's largest prediction market platform. Uses the Bayse REST API with Poke API as backend orchestration.

## Strategies Implemented

- **Kelly Criterion** — Size positions optimally based on edge and bankroll
- **Arbitrage Detection** — Spot mispriced markets where Yes + No < 1 (implied probability gap)
- **Market Making** — Provide liquidity by quoting both sides and capturing spread
- **Bayesian Prior Update** — Update market beliefs dynamically as new information arrives

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
| `BAYSE_API_KEY` | Your Bayse Markets API key (from app settings) |
| `BAYSE_BASE_URL` | API base URL (`https://relay.bayse.markets`) |
| `POKE_API_KEY` | Your Poke API key for backend orchestration |
| `POKE_WEBHOOK_URL` | Poke webhook endpoint for trade signals |
| `DRY_RUN` | Set to `true` to simulate trades without real money |
| `BANKROLL` | Total capital allocated to the bot (USDC) |
| `MAX_POSITION_FRACTION` | Max fraction of bankroll per trade (default: 0.05) |
| `MIN_EDGE` | Minimum edge threshold before trading (default: 0.03) |

## Running the Bot

```bash
# Run the full trading loop
python bot/main.py

# Run only the market scanner (no trades)
python bot/main.py --scan-only

# Run a specific strategy
python bot/main.py --strategy kelly
python bot/main.py --strategy arbitrage
python bot/main.py --strategy market-making
```

## Architecture

```
medes-et-bayse/
├── bot/
│   ├── main.py              # Entry point + trading loop
│   ├── bayse_client.py      # Bayse Markets REST API client
│   ├── poke_client.py       # Poke API client (backend orchestration)
│   ├── strategies/
│   │   ├── kelly.py         # Kelly Criterion position sizing
│   │   ├── arbitrage.py     # Arbitrage detection & execution
│   │   └── market_maker.py  # Market-making spread strategy
│   └── utils/
│       ├── bayesian.py      # Bayesian belief updating
│       └── risk.py          # Risk management helpers
├── tests/
│   └── test_strategies.py
├── .env.example
├── requirements.txt
└── README.md
```

## Poke Recipe Integration

This bot is designed to be triggered via Poke. The suggested Poke Recipe:

1. **Trigger**: Cron every 5 minutes (or Poke email automation)
2. **Action**: Call `POST /run-cycle` on the bot's webhook
3. **Output**: Poke notifies you of any trades executed or opportunities found

See `bot/poke_client.py` for the full webhook interface.

## Disclaimer

This bot is for educational purposes. Prediction markets carry financial risk. Only trade with funds you can afford to lose.

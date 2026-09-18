# Signals

One feed for everything that happens to the companies you watch — pulled from the
original public sources when they publish, not when someone writes it up.

Personal tool. Public data only. Finds signals; makes no decisions.

## Run it

    cp .env.example .env      # set SEC_USER_AGENT to a real contact address
    make up                   # postgres, redis, worker, api
    make migrate && make seed
    open http://localhost:8000

The worker polls 06:00–22:00 ET on trading days and idles otherwise. A quiet feed
is the normal state: forty watched companies mostly do nothing, and the design
target is under twenty flags a day. If a source stops polling, that appears in
the feed as a flag of its own.

## What it watches

| Source | Signal | How |
|---|---|---|
| EDGAR 8-K | Material events, scored by item number | index poll, 2 s — items come free in the index |
| EDGAR Form 4 | Open-market insider buys; grants and sales score 0 | index poll + one document fetch per watched filing |
| EDGAR 13D/G | Activist (85) vs passive (30) stakes | index poll, 4 s |
| Trading halts | News pending, regulatory, volatility | Nasdaq Trader RSS, 10 s — covers NYSE/Arca/AMEX too |
| Prices | `price_at` on every flag, weekly change, earnings dates | yfinance, best-effort, never blocks a flag |

Edit `config/watchlist.yml`, then `make seed && docker compose restart worker`.

## Layout

    src/signals/parsers/    pure: bytes in, dataclasses out. No IO, no clock.
    src/signals/scoring/    pure: one 0–100 scale, every constant in tables.py
    src/signals/adapters/   fetch + normalize, one per source
    src/signals/pipeline/   runner, processor, cluster promotion, watchdog, prices
    src/signals/store/      the only SQL in the project (queries.py)
    src/signals/api/        FastAPI: /api/feed /api/watchlist /api/stats /ws
    web/                    React feed + rail
    tests/fixtures/         real captured payloads; tests never touch the network

The purity of `parsers/` and `scoring/` is enforced by import-linter, not convention.

`STATUS.md` has the build log: what was measured live, where the original spec
turned out to be wrong, and what has not yet been observed in production.

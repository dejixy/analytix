# Analytix

**Why did this price move?** Analytix is a real-time market microstructure tool for Hyperliquid perps. It ingests live trades, the L2 order book and funding/open-interest data for several coins at once (ETH, BTC, SOL and HYPE by default, switchable in the top bar). It then explains each move over 1m, 5m and 15m windows in plain language, for example:

> **ETH −1.33% in 1m** — driven by a long-liquidation-style cascade (22 sweeps, $3.79M) and aggressive selling (92% of taker volume)
> · A significant move: 15.0× the typical 1m move (±9 bps)
> · Sellers took 92% of $8.59M taker volume (1157 fills, 3.9× normal pace)
> · Near-touch asks +28% ($808K → $1.03M), bids −64% ($888K → $317K); the book now leans 53% toward asks
> · Open interest −1.49% while price fell: crowded longs being flushed. Funding +28.8% APR
>
> *(Output from the synthetic demo session.)*

---

## Quick start

```bash
# backend
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
python -m pip install -r requirements.txt

# frontend (already built in frontend/dist; rebuild only after editing the UI)
cd frontend && npm install && npm run build && cd ..

# live Hyperliquid feed
python -m uvicorn api.app:app --port 8000
# → http://localhost:8000
```

To try it without a network connection, replay the synthetic demo session. It is generated on first run:

```bash
ANALYTIX_MODE=replay ANALYTIX_REPLAY_SPEED=10 python -m uvicorn api.app:app --port 8000
```

On Windows PowerShell, set the variables first with `$env:ANALYTIX_MODE="replay"; $env:ANALYTIX_REPLAY_SPEED="10"`, then run the same `python -m uvicorn …` line.

For frontend development with hot reload, run `npm run dev` in `frontend/` and open http://localhost:5173. It proxies `/api` and `/ws` to :8000.

Run the tests with `python -m pytest`. There are 37 tests, including an end-to-end replay that checks the engine finds the planted liquidation cascade, short squeeze and absorption.

---

## Architecture

The backend is organised as **pipeline stages**, not request/response layers:

```
Hyperliquid WS ──► ingestion/ ──► state.py ──────────► engine/ ──► api/ ──► React dashboard
 (or a replay)     parse raw       RingBuffers         slice per   REST +   (WebSocket push,
                   messages        (events) +          window,     /ws      2×/sec)
                                   latest book/ctx     run signals,
                                   (state)             explain, log
                                        │                  ▲
                                        └──► signals/ ─────┘  pure functions:
                                                             WindowSlice → SignalResult
```

| Folder / file | Job |
|---|---|
| `config.py` | **Single source of truth.** `WINDOWS`, thresholds and modes. Add `"1h": 3600` and every layer picks it up. |
| `models/` | Frozen dataclasses: `Trade`, `OrderBook`/`BookSummary`, `AssetContext`, `AggressiveOrder`, `SignalResult`, `Explanation`, `MoveEvent`. |
| `buffer/ringBuffer.py` | A time-evicting buffer on the **exchange clock**. `T` is bound to a `Timestamped` Protocol. `window()` stops early. Out-of-order items are rejected. |
| `state.py` | `MarketState`. Events (trades, book summaries, funding ticks) are **buffered**. State (the book, the latest context) is **replaced**. Derived events such as `BookSummary` and `AggressiveOrder` are built once, on arrival. |
| `ingestion/` | `hyperliquidClient.py` (live, with reconnect and ping), `replay.py`, `recorder.py`, `parsers.py` (raw JSON → models), `feedStatus.py` (the one place the **wall clock** is used, to detect a stale feed) and `synthetic.py` (the scripted demo market). |
| `signals/` | One pure function per signal. They are registered in `signals/__init__.py`. |
| `engine/` | `analyzer.py` runs once per exchange-second: it computes the baseline and window slices, then calls the signals and the explainer. `explainer.py` ranks drivers and writes the narrative. `eventLog.py` records significant moves at their peak. |
| `pipeline.py` | One definition of "process a message", shared by the API, the scripts and the tests. |
| `api/` | FastAPI: REST routes, the `/ws` push, the runtime (source and broadcaster) and serializers. |
| `scripts/` | `record.py` (capture live sessions), `replayReport.py` (print every significant move in a recording) and `generateSample.py`. |
| `frontend/` | React + Vite dashboard. |

### Design decisions worth defending in an interview

- **Exchange clock, not wall clock.** Every window is measured against the newest exchange timestamp seen. Laptop clock skew can't distort windows, and a recorded session replays to identical results. Only ingestion looks at wall time, to flag a dead feed.
- **Signals are pure functions.** Each one takes a window slice and returns a common `SignalResult` shape, so the explainer can rank them without knowing how they're computed. Adding a signal is one file plus one line in the registry.
- **Significance is relative.** A move is scored against the market's own recent volatility: `z = move / (σ₁ₛ·√seconds)`. Ten bps is a big deal on a sleepy Sunday and noise during CPI.
- **Derived events are computed at ingestion.** Fills are stitched into taker orders once, when they arrive, instead of for every window on every tick. That made the engine about 6× faster.
- **Each timeframe tells its own story.** Driver weights depend on the horizon. Sweeps and pulled liquidity explain seconds to minutes, so they lead the 1m thesis. Open interest and funding explain slower moves, so they lead the 15m thesis. Windows longer than a minute also report the move's *shape*: one sharp minute (a shock) or a steady grind (a trend).
- **One socket, many coins.** Live mode subscribes every coin in `ANALYTIX_COINS` on a single WebSocket and routes each message to its coin's own pipeline. The browser picks a coin with `/ws?coin=BTC`.
- **Hysteresis in the event log.** An episode opens at 2.5σ and stays open while the move is still ≥1.5σ. A move hovering at the threshold is logged once, not five times.

---

## The signals

| Signal | Question it answers | Bullish when |
|---|---|---|
| **Order flow** (`volumeImbalance.py`) | Who was the aggressor? Taker buy vs sell notional, scaled by activity versus normal | buyers dominate taker volume |
| **Book depth** (`depthDelta.py`) | How did resting liquidity near the touch (±25 bps) change over the window? | asks thin or bids stack up |
| **Forced flow** (`liquidations.py`) | Were there large multi-level sweeps, or chains of them (cascades)? | buy sweeps and short-liquidation-style cascades |
| **Funding & OI** (`funding.py`) | Who is positioned? The OI change is paired with the price move to read the 4-quadrant table below. Funding shows the crowded side. | *descriptive: it explains the move rather than pushing against it* |

|  | OI up | OI down |
|---|---|---|
| **price up** | new longs opening | shorts covering (squeeze) |
| **price down** | new shorts opening | longs closing / being flushed |

The explainer also calls out **absorption**: heavy one-sided aggression that *lost* (sellers at 72% while price held), which usually means a patient passive counterparty.

### Honest limitations

- **Liquidations are inferred, not confirmed.** Hyperliquid's public feed doesn't label liquidations, because a market liquidation executes as an ordinary taker order. Analytix detects liquidation-*style* cascades from sweep footprints. If you know liquidator addresses, set `ANALYTIX_LIQUIDATORS=0xabc…,0xdef…` and matching trades are flagged as confirmed.
- **History is 15 minutes plus a margin.** Older moves live on only in the event log (the last 200 events, in memory). Persisting them is on the roadmap.
- **The synthetic session is a toy market.** It is scripted so the tests have known answers. Record real sessions with `python -m scripts.record --minutes 60` and replay those.
- **The live client was written against the documented message formats**, and the parsers are tested against those shapes. Watch the first live run for surprises (see the checklist below).

---

## Configuration (environment variables)

| Variable | Default | |
|---|---|---|
| `ANALYTIX_MODE` | `live` | `live` or `replay` |
| `ANALYTIX_COINS` | `ETH,BTC,SOL,HYPE` | coins to watch in live mode (names are case-sensitive, e.g. `kPEPE`) |
| `ANALYTIX_COIN` | first of `ANALYTIX_COINS` | the coin the dashboard opens on |
| `ANALYTIX_REPLAY_FILE` | `data/sampleSession.jsonl` | a recording from `scripts/record.py` |
| `ANALYTIX_REPLAY_SPEED` | `4` | playback multiplier |
| `ANALYTIX_REPLAY_LOOP` | `1` | `0` to stop at the end |
| `ANALYTIX_RECORD_FILE` | — | in live mode, also record raw messages here |
| `ANALYTIX_LIQUIDATORS` | — | comma-separated addresses |

Thresholds live in `config.py`.

## API

| Endpoint | Returns |
|---|---|
| `GET /api/health` | feed status and engine tick count |
| `GET /api/config` | watched coins, windows and mode |
| `GET /api/snapshot?coin=BTC` | everything the dashboard shows for one coin |
| `GET /api/explain/{1m\|5m\|15m}?coin=BTC` | one window's explanation, drivers and signals |
| `GET /api/events?coin=BTC&window=1m&limit=20` | significant moves, newest first |
| `WS /ws?coin=BTC` | a snapshot on connect, then 2× per second; send `{"coin": "SOL"}` to switch |

`coin` defaults to `ANALYTIX_COIN` everywhere.

## First live run checklist

1. `python -m uvicorn api.app:app`. The badge should say **Live** within a few seconds.
2. `/api/health`: `messages` should be climbing and `engine_runs` should tick once per second.
3. Wait 15 minutes for the 15m window to fill. Until then the cards say "warming up".
4. Record a session (`python -m scripts.record --minutes 30`) and run `python -m scripts.replayReport <file> --coin ETH`. Check whether the explanations match what you saw.
5. Tune `config.py`. The sweep floor (`MIN_SWEEP_NOTIONAL`) and `SIGNIFICANT_Z` are the first knobs to adjust against real ETH flow.

## Roadmap

- Correlate spot moves with prediction-market odds (e.g. Polymarket ETH price markets)
- Persist the event log (SQLite or Postgres) and add a "what happened at 14:32?" query over history
- Longer windows (1h, 4h): add them to `WINDOWS`, and the buffers follow via `HISTORY_SECONDS`
- Per-coin thresholds (a $50K sweep floor is large for SOL, small for BTC)

# Analytix

**Why did this price move?** Analytix is a real-time market microstructure tool for Hyperliquid perps. It ingests live trades, the L2 order book and funding/open-interest data for several coins at once (ETH, BTC, SOL and HYPE by default, switchable in the top bar). It then explains each move over 1m, 10m, 60m, 6h, 12h, 24h and 1w windows in plain language, for example:

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

Run the tests with `python -m pytest`. There are 55 tests, including an end-to-end replay that checks the engine finds the planted liquidation cascade, short squeeze and absorption.

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
| `models/barModel.py` | One-minute `Bar`s — the storage tier for 6h … 1w windows. |
| `storage/barStore.py` | Saves live bars to SQLite (`data/bars.sqlite`) so the long windows survive restarts. |
| `ingestion/backfill.py` | Downloads ~a week of 5m price candles and funding history from Hyperliquid's REST API on start. |
| `buffer/ringBuffer.py` | A time-evicting buffer on the **exchange clock**. `T` is bound to a `Timestamped` Protocol. `window()` stops early. Out-of-order items are rejected. |
| `state.py` | `MarketState`. Events (trades, book summaries, funding ticks) are **buffered**. State (the book, the latest context) is **replaced**. Derived events such as `BookSummary` and `AggressiveOrder` are built once, on arrival. |
| `ingestion/` | `hyperliquidClient.py` (live, with reconnect and ping), `replay.py`, `recorder.py`, `parsers.py` (raw JSON → models), `feedStatus.py` (the one place the **wall clock** is used, to detect a stale feed) and `synthetic.py` (the scripted demo market). |
| `signals/` | One pure function per signal. They are registered in `signals/__init__.py`. |
| `engine/` | `analyzer.py` runs once per exchange-second: it computes the baseline and window slices, then calls the signals and the explainer. `explainer.py` ranks drivers and writes the narrative. `eventLog.py` records significant moves at their peak. |
| `engine/` (trading reads) | `summary.py` (the card rows), `engineFlow.py` (TWAP slices vs liquidations from engine-executed fills), `impact.py` (flow efficiency), `levels.py` (defended levels and breaks), `cascades.py` (OI check and recovery), `walls.py` (real vs pulled walls), `positioning.py` (funding/OI percentiles). See "The timeframe cards" below. |
| `pipeline.py` | One definition of "process a message", shared by the API, the scripts and the tests. |
| `api/` | FastAPI: REST routes, the `/ws` push, the runtime (source and broadcaster) and serializers. |
| `api/planner.py` | serves `GET /api/plan`: candles, max leverage, fitted models (refit every 15 minutes) and cached paths. |
| `scripts/` | `record.py` (always-on recording to hourly files), `replayReport.py` (print every significant move in a recording), `study.py` (what price did after each signal) and `generateSample.py`. |
| `frontend/` | React + Vite dashboard. |

### Design decisions worth defending in an interview

- **Exchange clock, not wall clock.** Every window is measured against the newest exchange timestamp seen. Laptop clock skew can't distort windows, and a recorded session replays to identical results. Only ingestion looks at wall time, to flag a dead feed.
- **Signals are pure functions.** Each one takes a window slice and returns a common `SignalResult` shape, so the explainer can rank them without knowing how they're computed. Adding a signal is one file plus one line in the registry.
- **Significance is relative.** A move is scored against the market's own recent volatility: `z = move / (σ₁ₛ·√seconds)`. Ten bps is a big deal on a sleepy Sunday and noise during CPI.
- **Derived events are computed at ingestion.** Fills are stitched into taker orders once, when they arrive, instead of for every window on every tick. That made the engine about 6× faster.
- **Each timeframe tells its own story.** Driver weights depend on the horizon. Sweeps and pulled liquidity explain seconds to minutes, so they lead the 1m thesis. Open interest and funding explain slower moves, so they lead the 60m thesis. Windows longer than a minute also report the move's *shape*: one sharp minute (a shock) or a steady grind (a trend).
- **One socket, many coins.** Live mode subscribes every coin in `ANALYTIX_COINS` on a single WebSocket and routes each message to its coin's own pipeline. The browser picks a coin with `/ws?coin=BTC`.
- **Two data tiers.** Windows up to an hour read every trade and book update. The 6h … 1w windows read one-minute bars rolled up as data arrives, because a week of raw trades for four coins wouldn't fit in memory. Price history for the long windows is backfilled from Hyperliquid's candles; order flow, depth and OI can't be (the API has no history of them), so each long card shows how much of its window that data covers.
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

### The timeframe cards

Each card shows the five readings that matter most for its horizon: a number for the pro, a one- or two-word verdict for everyone else, and the full working when you hover a row (or in the Why panel's "At a glance" list).

| Timeframe | Rows | Why these |
|---|---|---|
| **1m** (execution) | Flow · Who · Forced · Book · Liquidity | who's hitting the book right now, whether it's liquidations, and what a big order would cost |
| **10m** (scalp) | Flow · Who · Trend · VWAP · Book | whether the pressure is working, who it is, and whether price is trending or chopping around value |
| **60m** (session) | Trend · VWAP · Flow · Who · Positioning | the structure of the hour, plus who's opening or closing positions |
| **6h / 12h / 24h / 1w** (swing) | Trend · VWAP · Positioning · Funding · Flow | trend and value over the day, positioning, and what it costs to hold |

- **Flow**: aggressive buy vs sell share, net dollars, and what that flow *did*. The tag compares price impact with what that much net flow normally buys: *absorbed*, *against flow*, *outsized*, or the pace of trading.
- **Who**: Hyperliquid prints the wallet behind every trade, so this row shows how concentrated the aggressive side is: one wallet, a **TWAP**, or many traders. Hover it for the wallet, its size, and its slice cadence. A "95% sell, flat price" tape that's really one TWAP being absorbed is a very different market from broad selling.
- **Forced**: sweeps, cascades and **engine-executed liquidations**. Fills the exchange's engine generates itself carry an all-zero transaction hash; the classifier (`engine/engineFlow.py`) separates TWAP slices from forced closes using Hyperliquid's own rules:
  - **TWAP**: a fixed interval of 30s or more, with similar slice sizes.
  - **Forced close**: many accounts on the same side within the same few seconds, or a 20% partial liquidation followed by the rest.
  - Anything ambiguous is left unclassified.
- **Book**: bid/ask split of resting depth near the price, how it changed, and whether big walls on the stacking side keep getting pulled ("bait?").
- **Liquidity**: slippage of a large market order (sized to the coin) right now, buy and sell, compared with its usual cost over the last 30 minutes.
- **Trend**: efficiency ratio (net move ÷ distance travelled over 30 equal steps; a random market scores about 0.18) and position in the range. Thresholds are calibrated on simulated random walks:
  - "uptrend/downtrend" fires on about 4% of pure-noise windows and catches about 93% of real drifts.
  - "chop" fires on about 4% of noise.
- **VWAP**: price vs the window's volume-weighted average price. The distance is scored against how far a random market typically sits from its own VWAP, so "stretched" means further than about 95% of random moments.
- **Positioning**: OI change in % and $, ranked against every OI move of the same timeframe, and who it says is moving (longs in, short cover, shorts in, longs out).
- **Funding**: the rate, what holding a position for that timeframe costs, and whether one side is crowded. A side counts as crowded only when funding is both a high percentile for the week (mid-rank, so the usual floor rate isn't misread) and meaningfully above the floor.

The footer compares the window's high-low range with the usual range for that timeframe. Rows from a timeframe that hasn't filled yet are dimmed, and long windows say how much of the window their live flow covers.

### What each read is for

The four signals say *what happened*. These reads turn them into things a trader can act on or check:

| Read | Where you see it | How it's worked out | Why it matters |
|---|---|---|---|
| **Flow efficiency** | card footer: `impact 0.3× · absorbed`; Why panel | The coin's normal price impact (bps per $1M of net taker flow) is fitted per timeframe from live minute bars. Each window's actual move ÷ the move its net flow normally buys. | Under 0.35× or the wrong way means the aggressors were absorbed; over 2.5× means a thin book or a move led from other venues. "76% sell" alone can't tell you that. Each timeframe needs its own history: ~20 min of live bars for 1m, ~40 for 10m, ~4 h for 60m (saved across restarts). |
| **Defended levels** | headline: `bids absorbed it at 2,650.40`; dashed lines on the chart; LEVEL events | The price bin (±2 bps) that absorbed the most aggressive flow without price trading beyond it afterwards. Tracked from 1m/10m/60m; a level breaks when price stays half a normal 1-minute move through it for 10 s. | A level to lean on (stop just beyond it) and a trigger (it breaking). Only levels that held 3+ minutes log a break; breaks within 30 s on one side are one event. |
| **Cascade check** | Forced flow summary; CASCADE events and pop-ups | Open interest from before the first sweep vs a reading taken 15 s+ after the last. OI falling by ≥ 50% of the cascade's size → likely liquidations; flat or rising → likely one large trader. Then price is followed for 15 min. | Forced selling that's absorbed tends to snap back; repositioning keeps going. "Won back 60% in 5m" vs "kept going past the low" tells you which. Not proof: a liquidated long can sell into a new long's bid, leaving OI flat. |
| **Walls: real or pulled** | order book `wall` tags and `real · pulled` counts; Book depth summary | Levels ≥ 4× the median visible level and ≥ 2 sweeps' worth of USD, followed until they go. Eaten = at least half of what vanished was traded; pulled = vanished unfilled with price within 10 bps. | "Asks +94%" may be bait. If the stacking side keeps pulling its walls, the Book depth summary says so. Hyperliquid shows 20 levels a side, so this only sees walls near the touch. |
| **Percentiles** | card stat: `OI −1.40% · top 5%`; Positioning panel | Funding vs the past week's hourly readings; each window's OI change vs every OI move over the same timeframe in the saved live bars. | Normal for one coin is extreme for another. A percentile says whether this move is unusual for *this* coin. |
| **Timeframe agreement** | ▲/▼ on each tab; `Flow aligned ▲ 1m–60m` beside them | Which way aggressive flow leans (strength ≥ 0.25) on 1m, 10m, 60m. | Aligned flow is a trend; short-term flow pushing against the 60m is a pullback or a turn. |

### Running Analytix on the server

The recorder alone captures the feed. Running the whole backend on the server instead captures the same recordings *and* keeps the dashboard's history complete around the clock: the 24h and 1w cards have a full day and week of real flow and open interest, open-interest percentiles and price-impact calibration build up without gaps, and alerts can run 24/7. `deploy/analytix.service` does this. It listens only on the server itself; you view it through an SSH tunnel.

Switch over (on the server, after the recorder setup above):

```bash
sudo -u analytix git -C /opt/analytix pull
sudo -u analytix /opt/analytix/.venv/bin/pip install -r /opt/analytix/requirements.txt
sudo cp /opt/analytix/deploy/analytix.service /etc/systemd/system/
sudo systemctl disable --now analytix-recorder
sudo systemctl enable --now analytix
journalctl -u analytix -f          # a status line every 5 minutes
```

The recordings carry on in the same folder (a switch inside one hour appends to that hour's file). View the dashboard from your laptop:

```bash
ssh -N -L 8001:127.0.0.1:8000 you@SERVER_IP      # leave this running, then open http://localhost:8001
```

Port 8001 on your laptop so it doesn't clash with a local copy on 8000. Update later with the first two commands, then `sudo systemctl restart analytix`.

### Alerts

The **Alerts** button opens the alert settings. Alerts are checked every second for every coin, wherever Analytix runs (your server), and go to Telegram and, if you switch it on, to desktop notifications while the dashboard sits in a background tab.

| Alert | Fires when | Default |
|---|---|---|
| Liquidation cascade | a chain of forced flow, once its OI check is in | ≥ $1M liquidated or swept |
| Big liquidation | one account force-closed by the exchange (its 20% chunk and the rest added up) | ≥ $500K |
| New TWAP | a wallet starts slicing a big order | ≥ $1M an hour |
| Level break | a defended level gives way | on |
| Absorbed flow | heavy one-sided flow fails to move price on the 10m or 60m card | on |
| Volatility spike | the 10m range is ≥ N× its usual size | 2.5× |
| Price alerts | price crosses a level you set (once) | — |

Each alert type has a quiet time per coin (15 minutes by default), and nothing fires in the first five minutes after start-up, while the engine learns what's normal. A ping reads like:

> 🔻 **ETH long-liquidation cascade · $5.08M liquidated**
> 31 sweeps in 37s · price −1.45% · liquidations confirmed by OI
> now 2,648.95 (−0.70% in 10m) · 61% sell · net −$6.18M

**Telegram setup:** message @BotFather, send `/newbot`, paste the token into the Alerts panel, press Start in your new bot, then **Find chats** and switch your chat on. Only chats you switch on get alerts — anyone else who finds the bot gets nothing. Add the bot to a group or a channel to share alerts. Settings and the token live in `data/alerts.json` on the machine running Analytix (never in git; the API never returns the token). Replays never send to Telegram.

### Plan a trade

The **Plan a trade** button opens the position planner. Pick long or short, margin, leverage and how long you'll hold (15m … 1w). It answers, before you click buy:

- **Chance of liquidation before you exit**, the liquidation price (Hyperliquid's isolated-margin formula, maintenance = half the initial margin at the coin's max leverage), and a ladder of the same odds at 1×, 2×, 3×, 5×, 10× …
- **Safe leverage**: the most leverage that keeps that chance under 1% / 5% for this hold.
- **Where price trades before you exit**: round levels above and below with the chance price touches each, so you can see where a stop would be hit by noise and where a target is realistic.
- **When you close**: P&L percentiles (5th … 95th) after fees, slippage on the live book and funding, and the chance of being in profit.
- **Costs**: fees (0.045% in and out), slippage now, and funding expected over the hold (today's rate drifting back to the week's average).
- **Plain summary first**: one sentence on what the trade means ("5× long for 24h: liquidation is very unlikely. Expect about ±2.8% of movement, and to be 1.6% against you at some point (−$82). Costs $6."), and the leverage to drop to if the risk is over 1%.
- **Isolated or cross margin.** In cross (Hyperliquid's default), your whole account balance backs the position: liquidation is where the account runs out, and a liquidation takes the account.
- **The price cone**: where half and 90% of simulated paths are at each moment of the hold, with entry, stop, target and liquidation drawn on it; hover for the numbers and the chance of liquidation by then.
- **Bad cases**: the worst 1-in-100 result, the average of the worst 5% (expected shortfall), and the chance of losing half your margin or more.
- **Slippage beyond the visible book**: sizes the 20 visible levels can't fill are costed with the square-root impact law (≈ 0.7 × daily volatility × √(size ÷ 24h volume)) instead of being left out.
- **Stop and take-profit** (optional, % from entry): the chance each closes the trade first, or that neither does before you exit; what each is worth after costs; how long each typically takes; and the average result. If one candle reaches both, the loss is taken as first. A stop past the liquidation price is flagged, since liquidation would come first. With no directional view the average is roughly minus the costs whatever the bracket — it changes the shape of your results, not the average.

How the odds are made — filtered historical simulation, the method risk desks use for VaR (`engine/planner.py`):

1. The coin's own candles from Hyperliquid: hourly (~200 days) for holds over 6h, 5-minute (~17 days) for shorter ones. Each candle gives three moves from the previous close: to the close, the low and the high — wicks are what liquidate people.
2. Time of day and weekends are taken out (measured on the ~200 days of hourly candles, reused for the 5-minute model).
3. A GARCH(1,1) gives each candle's expected volatility; dividing by it leaves the coin's shape of surprise (fat tails, lopsided wicks) without the regime. How long volatility lingers is uncertain, so each path draws its own GARCH parameters from how well they fit.
4. Volatility is brought up to the moment through every candle since the fit and the move so far, so a crash ten minutes ago counts.
5. 20,000 paths draw random historical candles scaled to the volatility expected at each step; each path's worst point, best point and exit are read off.

**Checked on the coin's own history, every time.** For each coin and hold, the planner refits itself on the older 60% of the candles and walks through the newer 40% it never saw: at up to 200 past moments it predicts the chance of price reaching levels ½ to 3 typical moves away before the hold ends, then checks the candles. The drawer shows what it said against what happened ("it said 5.5%, it happened 4.7%"), how often its 90% range held, and the same check for a plain bell-curve model. If the model is wrong for a coin, you see it there.

No direction is assumed: historical drift is removed, so it sizes the room a trade needs, not which way price goes. Checked in `tests/test_planner.py` against brute-force simulation of known processes (touch odds within ~15% at 4h–3d; a 1-week 1-in-100 tail within 3×, the limit of what 200 days of data can tell). Assumptions shown with every plan: isolated margin; liquidation costs the whole margin plus entry costs; candle wicks are last-trade prices while liquidation uses the mark price, so wicks slightly overstate the risk. In replay mode, or before the candles load, it falls back to a rough fat-tailed walk and says so.

### Does any of it work? Measure it

`scripts/study.py` replays recordings and, using only what the engine knew at each moment, logs every time one of these reads fires and the direction it implies. It also logs every card tag on the 1m, 10m and 60m cards that leans one way ("absorbed", "TWAP", "stretched", "uptrend", "liq flush"…), in the direction the row leans. Then it checks the price 1, 5, 15 and 60 minutes later:

```bash
python -m scripts.record                 # leave it running: days, not hours
python -m scripts.study                  # every coin in data/recordings
python -m scripts.study data/recordings --coin ETH --min-n 30 --csv study.csv
```

```
14 session(s) · 212.5 hours · ETH, BTC, SOL, HYPE

signal                                     n  │ 1m  hit   avg    t  │ 5m  hit   avg    t  …
absorption (10m)                          41  │     58%  +2.1  +1.9 │     55%  +3.4  +1.2 …
card vwap: stretched (10m)                88  │     44%  -0.9  -1.4 │     41%  -2.6  -2.2 …
```

`hit` is the share that went the implied way, `avg` the mean move that way in bps, `t` the average ÷ its standard error. A card tag with a negative `avg` means fading it worked (above: price stretched above VWAP tended to come back). Under ~30 occurrences or with |t| < 2, it isn't known yet. Keep the reads that hold up across days and coins, and drop the ones that don't. (The numbers above are illustrative. The synthetic session is far too short to say anything.)

### Recording for the study

The recorder writes one file per hour to `data/recordings/`. The hour being written is plain JSONL, so a crash or power cut loses only the last moment; finished hours are gzipped in the background. Hourly files from one run are stitched back into one continuous session by the study; a gap of more than 10 minutes starts a fresh session. Recording pauses (and logs it) when free disk drops below 2 GB.

- **On your laptop:** `python -m scripts.record`, and keep the machine awake. Run it again after any interruption and it carries on. Or start the dashboard with `ANALYTIX_RECORD=1` and it records while you watch.
- **On a small Linux server** (any $4–6/month VPS, Ubuntu 22.04+), so it runs 24/7:

```bash
sudo apt install -y git python3-venv
sudo git clone https://github.com/dejixy/analytix /opt/analytix
cd /opt/analytix && sudo python3 -m venv .venv && sudo .venv/bin/pip install -r requirements.txt
sudo useradd -r -s /usr/sbin/nologin analytix && sudo chown -R analytix /opt/analytix
sudo cp deploy/analytix-recorder.service /etc/systemd/system/
sudo systemctl enable --now analytix-recorder
journalctl -u analytix-recorder -f          # one status line a minute
```

  Copy the recordings to your laptop for the study with `scp -r user@server:/opt/analytix/data/recordings data/` (or `rsync -av` to fetch only new hours).

The recorder prints how many MB the current hour has taken, so you can see your disk use per day after the first few hours.

### Honest limitations

- **Liquidations are classified, not labelled.** Hyperliquid's public feed doesn't say "liquidation". Fills the engine executes itself (no transaction hash) are TWAP slices, liquidations or auto-deleveraging, and `engine/engineFlow.py` tells them apart by Hyperliquid's own rules: TWAPs slice at a fixed interval in similar sizes; liquidations hit many accounts at once, or come as a 20% chunk then the rest within 30 s. Anything that fits neither is left unclassified. Sweeps by ordinary traders are checked against open interest ("likely liquidations" when OI fell with them). If you know liquidator addresses, set `ANALYTIX_LIQUIDATORS=0xabc…,0xdef…` and matching trades are flagged as confirmed.
- **None of the reads is a proven edge yet.** They are built on standard microstructure ideas (price impact, absorption, forced flow, spoofing), but whether each one predicts anything on Hyperliquid is an empirical question. `scripts/study.py` is how to answer it.
- **Long windows are only as complete as the app's uptime.** Price is backfilled, but order flow, depth and OI for the 6h … 1w windows build up while the app runs (saved across restarts). Long windows refresh every 15 seconds.
- **Older moves are kept in memory only.** They live on in the event log (the last 200 events). Persisting them is on the roadmap.
- **The synthetic session is a toy market.** It is scripted so the tests have known answers. Record real sessions with `python -m scripts.record` and replay those.
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
| `ANALYTIX_RECORD` | `0` | `1`: in live mode, also record the feed to hourly files (for the study) |
| `ANALYTIX_RECORD_DIR` | `data/recordings` | where those files go |
| `ANALYTIX_ALERTS_FILE` | `data/alerts.json` | alert settings, price levels and the Telegram bot token |
| `ANALYTIX_RECORD_FILE` | — | in live mode, also record raw messages to this one file |
| `ANALYTIX_LIQUIDATORS` | — | comma-separated addresses |
| `ANALYTIX_BACKFILL` | `1` | `0` to skip downloading price candles on start |
| `ANALYTIX_BARS_DB` | `data/bars.sqlite` | where live minute bars are saved (live mode only) |

Thresholds live in `config.py`.

## API

| Endpoint | Returns |
|---|---|
| `GET /api/health` | feed status and engine tick count |
| `GET /api/config` | watched coins, windows and mode |
| `GET /api/snapshot?coin=BTC` | everything the dashboard shows for one coin |
| `GET /api/explain/{1m\|10m\|60m\|6h\|12h\|24h\|1w}?coin=BTC` | one window's explanation, drivers and signals |
| `GET /api/events?coin=BTC&window=1m&limit=20` | significant moves, newest first |
| `GET /api/explain_at?t=<ms>&window=10m&coin=BTC` | "what happened at 14:32?": the explanation for the window ending at any moment still in memory (the last hour for 1m–60m, the last week for 6h+). Falls back to a shorter timeframe if the requested one reaches back past the data. This is what clicking the chart calls. |
| `GET /api/plan?side=long&margin=1000&leverage=5&hours=24&stop=2&target=4&coin=BTC` | the position planner: liquidation price and odds, safe leverage, touch odds, P&L percentiles at exit, costs, the model behind them, and (with `stop`/`target`, % from entry) the bracket's odds. Add `account=3000` for cross margin. The first answer for a coin and hold starts the history check (`calibration_pending: true`); it's included a moment later. |
| `GET /api/alerts` · `PUT /api/alerts` · `POST /api/alerts/levels` · `POST /api/alerts/telegram/{token,discover,test}` | alert settings (never the token), price alerts, Telegram connection and the latest alerts. |
| `WS /ws?coin=BTC` | a snapshot on connect, then 2× per second; send `{"coin": "SOL"}` to switch |

`coin` defaults to `ANALYTIX_COIN` everywhere.

## First live run checklist

1. `python -m uvicorn api.app:app`. The badge should say **Live** within a few seconds.
2. `/api/health`: `messages` should be climbing and `engine_runs` should tick once per second.
3. The 10m card fills after 10 minutes and the 60m card after an hour. The 6h … 1w cards show price immediately (backfilled) and their flow data fills in as the app runs. Until then the cards say "warming up".
4. Record a session (`python -m scripts.record --minutes 30`) and run `python -m scripts.replayReport <file> --coin ETH`. Check whether the explanations match what you saw.
5. Tune `config.py`. The sweep floor (`MIN_SWEEP_NOTIONAL`) and `SIGNIFICANT_Z` are the first knobs to adjust against real ETH flow.

## Roadmap

- Correlate spot moves with prediction-market odds (e.g. Polymarket ETH price markets)
- Persist the event log (SQLite or Postgres) and add a "what happened at 14:32?" query over history
- More windows: add them to `WINDOWS` in `config.py`; anything over an hour automatically uses minute bars
- Per-coin thresholds (a $50K sweep floor is large for SOL, small for BTC)

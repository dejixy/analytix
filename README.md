# Analytix

**Why did this price move?** Analytix is a real-time market microstructure tool for Hyperliquid perps. It ingests live trades, the L2 order book and funding/open-interest data for several coins at once (ETH, BTC, SOL and HYPE by default, switchable in the top bar). It then explains each move over 1m, 10m, 60m, 6h, 12h, 24h and 1w windows in plain language, for example:

> **ETH −1.74% in 1m**: driven by a long-liquidation cascade (31 big orders, $5.52M; $5.08M liquidated) and heavy selling (94% of market orders)
> · A significant move: 19.5× a typical 1m move (±0.09%). Range 2,645.95 to 2,692.85.
> · Sellers made up 94% of $10.50M in market orders (1,413 trades, 4.7× the usual pace).
> · Sell orders close to the price +5% ($1.03M → $1.08M), buy orders −66% ($1.06M → $359K). The book now leans 50% toward sellers.
> · Open interest −1.99% while price fell: crowded longs being forced out. Funding 27.9% a year (longs pay shorts, crowded).
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

Run the tests with `python -m pytest`. There are 171 tests, including an end-to-end replay that checks the engine finds the planted liquidation cascade, short squeeze and absorption.

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
| `models/barModel.py` | One-minute `Bar`s: the storage tier for 6h … 1w windows. |
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
| **10m** (scalp) | Flow · Who · Trend · Avg price · Book | whether the pressure is working, who it is, and whether price is trending or chopping around value |
| **60m** (session) | Trend · Avg price · Flow · Who · Positions | the structure of the hour, plus who's opening or closing positions |
| **6h / 12h / 24h / 1w** (swing) | Trend · Avg price · Positions · Funding · Flow | trend and value over the day, positioning, and what it costs to hold |

- **Flow**: market-order buy vs sell share, net dollars, and what that flow *did*. The tag compares the price move with what that much net buying or selling usually does: *price held*, *rose anyway* / *fell anyway*, *big reaction*, or how busy trading is (*busy*, *steady*, *quiet*).
- **Who**: Hyperliquid prints the wallet behind every trade, so this row shows how concentrated the aggressive side is: one wallet (*whale*), a **TWAP** bot, *few wallets* or *many wallets*. Hover it for the wallet, its size, and its slice cadence. A "95% sell, flat price" tape that's really one TWAP being absorbed is a very different market from broad selling.
- **Forced**: sweeps, cascades and **engine-executed liquidations**. Fills the exchange's engine generates itself carry an all-zero transaction hash; the classifier (`engine/engineFlow.py`) separates TWAP slices from forced closes using Hyperliquid's own rules:
  - **TWAP**: a fixed interval of 30s or more, with similar slice sizes.
  - **Forced close**: many accounts on the same side within the same few seconds, or a 20% partial liquidation followed by the rest.
  - Anything ambiguous is left unclassified.
- **Book**: bid/ask split of resting depth near the price (*more bids*, *more asks*, *balanced*), how it changed, and whether big walls on the stacking side keep getting pulled (*fake walls?*).
- **Liquidity**: what a large market order (sized to the coin) costs in slippage right now, in dollars, buy and sell, compared with its usual cost over the last 30 minutes (*thin*, *normal*, *deep*).
- **Trend**: how one-way the move was (efficiency ratio: net move ÷ distance travelled over 30 equal steps; a random market scores about 18%) and where price sits in the range (near the high, mid-range, near the low). Thresholds are calibrated on simulated random walks:
  - "uptrend/downtrend" fires on about 4% of pure-noise windows and catches about 93% of real drifts.
  - "choppy" fires on about 4% of noise. Otherwise it reads *sideways* or *quiet*.
- **Avg price** (VWAP): price vs the window's volume-weighted average price. The distance is scored against how far a random market typically sits from its own VWAP, so *far above* / *far below* means further than about 95% of random moments (otherwise *above average*, *below average* or *at average*).
- **Positions**: open interest (all open positions) change in % and $, ranked against every open interest move of the same timeframe, and who it says is moving (*new longs*, *shorts exit*, *new shorts*, *longs exit*, *long squeeze*, *short squeeze*).
- **Funding**: the rate, what holding a position for that timeframe costs, and whether one side is crowded. A side counts as crowded only when funding is both a high percentile for the week (mid-rank, so the usual floor rate isn't misread) and meaningfully above the floor.

The footer compares the window's high-low range with the usual range for that timeframe. Rows from a timeframe that hasn't filled yet are dimmed, and long windows say how much of the window their live flow covers.

### What each read is for

The four signals say *what happened*. These reads turn them into things a trader can act on or check:

| Read | Where you see it | How it's worked out | Why it matters |
|---|---|---|---|
| **Flow efficiency** | card footer: `moved 0.3× usual · price held`; Why panel | The coin's normal price impact (bps per $1M of net taker flow) is fitted per timeframe from live minute bars. Each window's actual move ÷ the move its net flow normally buys. | Under 0.35× or the wrong way means the aggressors were absorbed; over 2.5× means a thin book or a move led from other venues. "76% sell" alone can't tell you that. Each timeframe needs its own history: ~20 min of live bars for 1m, ~40 for 10m, ~4 h for 60m (saved across restarts). |
| **Defended levels** | headline: `sellers pushed, but buyers held at 2,650.40`; dashed lines on the chart; LEVEL events | The price bin (±2 bps) that absorbed the most aggressive flow without price trading beyond it afterwards. Tracked from 1m/10m/60m; a level breaks when price stays half a normal 1-minute move through it for 10 s. | A level to lean on (stop just beyond it) and a trigger (it breaking). Only levels that held 3+ minutes log a break; breaks within 30 s on one side are one event. |
| **Cascade check** | Forced flow summary; CASCADE events and pop-ups | Open interest from before the first sweep vs a reading taken 15 s+ after the last. OI falling by ≥ 50% of the cascade's size → likely liquidations; flat or rising → likely one large trader. Then price is followed for 15 min. | Forced selling that's absorbed tends to snap back; repositioning keeps going. "Won back 60% in 5m" vs "kept going past the low" tells you which. Not proof: a liquidated long can sell into a new long's bid, leaving OI flat. |
| **Walls: real or pulled** | order book `wall` tags and `real · pulled` counts; Book depth summary | Levels ≥ 4× the median visible level and ≥ 2 sweeps' worth of USD, followed until they go. Eaten = at least half of what vanished was traded; pulled = vanished unfilled with price within 10 bps. | "Asks +94%" may be bait. If the stacking side keeps pulling its walls, the Book depth summary says so. Hyperliquid shows 20 levels a side, so this only sees walls near the touch. |
| **Percentiles** | Positions row: `−1.40% (−$21.3M) · top 5%`; Positioning panel | Funding vs the past week's hourly readings; each window's open interest change vs every one over the same timeframe in the saved live bars. | Normal for one coin is extreme for another. A percentile says whether this move is unusual for *this* coin. |
| **Timeframe agreement** | ▲/▼ on each tab; `Buying on 1m and 10m, selling on 60m` beside them | Which way aggressive flow leans (strength ≥ 0.25) on 1m, 10m, 60m. | Aligned flow is a trend; short-term flow pushing against the 60m is a pullback or a turn. |
| **Where it started** | card footer: `started on Binance`, `moved with Binance`, `started on Hyperliquid`, `Hyperliquid only`; Why panel; cascade and volatility alerts | Binance and Bybit perps' best bid and ask (public feeds, recorded alongside Hyperliquid's) over the move: the whole 1m window, or the sharpest minute of 10m and 60m. If the other exchange moved under 30% as far, the move was Hyperliquid's own. Otherwise the standard lead-lag test: both prices on a 0.25s grid, the shift (up to ±10s) where their changes match best is how far one led. ≥ 1s either way names the leader; less is "moved with". No good match, no claim. | A market-wide move arriving here and a Hyperliquid-only move (a local cascade, a whale) are different trades: the second tends to get pulled back toward other exchanges' prices by arbitrage. |

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

Every 5 minutes the log also prints a speed line: `screen delay` (how late a task that should run every quarter second actually ran; small means nothing is waiting), and how long the engine, the scanner and each round of browser updates take. `/api/health` shows the same numbers. If the server ever stalls for more than a second, a `FREEZE` line in the log shows exactly which lines of code it was stuck on (and what other threads were doing), so a stall can be fixed at its source. The recordings carry on in the same folder (a switch inside one hour appends to that hour's file). View the dashboard from your laptop:

```bash
ssh -N -L 8001:127.0.0.1:8000 you@SERVER_IP      # leave this running, then open http://localhost:8001
```

Port 8001 on your laptop so it doesn't clash with a local copy on 8000. Update later with the first two commands, then `sudo systemctl restart analytix`.

### Alerts

The **Alerts** button opens the alert settings. Alerts are checked every second for every coin, wherever Analytix runs (your server), and go to Telegram and, if you switch it on, to desktop notifications while the dashboard sits in a background tab.

| Alert | Fires when | Default |
|---|---|---|
| Liquidation cascade | a run of forced selling or buying, once open interest confirms it | ≥ $1M liquidated or swept |
| Big liquidation | one account force-closed by the exchange (its 20% chunk and the rest added up) | ≥ $500K |
| New TWAP | a wallet starts buying or selling a big amount in small slices | ≥ $1M an hour |
| Broken price level | a price that buyers or sellers defended for minutes gives way | on |
| Heavy trading, price held | lots of buying or selling fails to move price on the 10m or 60m card | on |
| Volatility spike | the last 10 minutes covered ≥ N× the normal range | 2.5× |
| Price alerts | price crosses a level you set (once) | you add them |
| Position risk | a watched wallet's chance of liquidation in the next 24h, or the next hour, goes above N% | 5% |
| Unusual coin | a coin outside your four tabs (in More coins) gets N× past its "worth a look" line; once per episode, at most 3 at a time | 3× |

Each alert type has a quiet time per coin (15 minutes by default), and nothing fires in the first five minutes after start-up, while the engine learns what's normal. A ping reads like:

> 🔻 **ETH: $5.08M of longs liquidated in 37s**
> 31 big sell orders in a row pushed price −1.45%. Open interest fell, so these were real liquidations.
> Now 2,648.95 (−0.70% in 10m). Last 10m of market orders: 61% sell · net −$6.18M.

Every alert says exactly what happened and where: "ETH fell through 2,667.90" (buyers had defended it for 8 min, soaking up $1.21M of selling), never just "level broke".

**Telegram setup:** message @BotFather, send `/newbot`, paste the token into the Alerts panel, press Start in your new bot, then **Find chats** and switch your chat on. Only chats you switch on get alerts; anyone else who finds the bot gets nothing. Add the bot to a group or a channel to share alerts. Settings and the token live in `data/alerts.json` on the machine running Analytix (never in git; the API never returns the token). Replays never send to Telegram.

### More coins (the scanner)

ETH, BTC, SOL and HYPE have their own tabs. **More coins**, after them, ranks every other Hyperliquid perp too (and those four) by what's unusual right now, each judged against its own normal:

| Reading | Compared with | "Worth a look" at |
|---|---|---|
| Price move, 15m and 1h | a normal move for that coin (its last 7 days of hourly candles) | 2× normal |
| Volume, last 24h | its average day | 2× |
| Open interest change, 1h | how much it usually moves in an hour (learned as the app runs) | 2.5× usual |
| Funding | a yearly rate, either side | 40% a year |
| Price vs index | the spot index | 0.15% |
| Trades (the 30 most traded coins) | a normal hour's volume, with a dollar minimum | 15m volume at 3× normal; liquidations, a run of big orders or a TWAP bot worth 5 to 10% of a normal hour (and at least $100K liquidated, a $250K run, or $250K an hour of TWAP); 70%+ of market orders on one side with $250K+ traded |

A coin's score is its biggest reading, and every reading past "worth a look" becomes a plain reason next to it ("price +9.3% in 1h, 8.6× a normal hour", "open interest +23.4% in 1h", "TWAP bot selling about $800K an hour"). Coins trading under $500K a day are listed but never ranked above real activity. Filter by kind of reading (the list is then ranked by that kind, and its reasons come first), search, or sort by any column.

Clicking ETH, BTC, SOL or HYPE switches to its tab. Clicking any other coin opens its full dashboard (cards, Why panel, chart, order book, event log) inside More coins: it subscribes to that coin's live feed, loads its price history for the long timeframes, and closes again after 10 minutes with nobody watching (3 at a time). Those coins don't send alerts or get recorded.

The data: one `metaAndAssetCtxs` call every 10 seconds covers every coin's price, index, funding, open interest and 24h volume; hourly candles load one coin every 5 seconds (busiest first) and refresh every 6 hours; a second, unrecorded WebSocket follows the trades of the 30 most traded coins and any opened coin. All well inside Hyperliquid's rate limits. Live mode only (`engine/scanner.py`, `api/scanner.py`).

### Outcomes (prediction markets)

The **Outcomes** tab, after More coins, covers Hyperliquid's prediction markets (HIP-4): every market listed on Hyperliquid's books, from **Outcome** (outcome.xyz) and **trade.xyz** (its Events markets) today, and any other venue that lists later. Each market has a Yes and a No side, and a contract pays $1 if it happens, so a Yes price of 0.62 reads as 62% likely. Filter by venue (All, Outcome, trade.xyz) and by kind (price, sports, other), search, and sort by volume, closing soon, gap to fair odds or 24h move.

The list has two parts. **Price markets** are grouped by the asset they settle on (ETH, BTC, SILVER…), each as a ladder: when it closes, the line, the bid and ask, the fair odds and the edge (how far fair odds sit outside the bid and ask: positive means Yes looks cheap, negative rich; a last trade or mark is never used, since on a thin market it can be days old). A **ladder check** flags when a higher line's Yes sells for more than a lower line's Yes costs on the same asset, venue and closing time: Yes on the lower plus No on the higher then pays at least $1 whatever happens, for less. **Events** are one card each: a question's outcomes (a match: home win, away win, draw, or "none of these"; a tournament or award's candidates) with an odds bar, the odds and the 24h change, or a single market's two sides. A card's footer says whether the odds add up, when there's a locked-in edge, or when nobody is quoting. A market with no orders and no trades today shows no odds rather than a placeholder price. Markets past their closing time are hidden unless **Closed** is on.

What Analytix adds on top of the raw market:

- **Fair odds for price markets** ("ETH above 2,538.8 at 18:30 UTC", "SILVER above 60.7"): the chance the perp ends past the line, from its live price and how much it moves between now and the close, hour by hour of the week: four weeks of hourly candles give each hour of the day its own volatility, weekdays and weekends apart, so a stock-index perp expects the session that lies ahead instead of averaging it with the hours the market is shut (one figure over the whole clock understated a Monday SP500 market's move by about half). Where Cboe publishes a volatility index (VIX for SP500, VXN for XYZ100, VXSLV for silver, GVZ for gold) the profile is scaled to it, so the level is the options market's own forecast; within 6 hours of the close, the last 12 hours of 1-minute candles take over when they're busier. No trend assumed. The gap between that and the market's odds is shown in points, and the list notes when Yes looks cheap or rich by 5 points or more. "Touch" markets use the chance of reaching the line at any point before the close.
- **The add-up check for questions.** Exactly one outcome of a question ends Yes, so the Yes odds should add up to 100%. When Yes on every outcome costs under $1 (or No on every outcome costs less than it pays), the difference is locked in, and the note says how much per set and how many sets the best prices allow, before fees.
- **Who is trading** (on a market's page): the busiest wallets and which side each backed, with plain notes when two wallets are only trading with each other (that volume isn't real interest), when a wallet trades with itself, or when one wallet does most of the buying on a side.
- **Why the odds moved:** one line over the last hour, e.g. "Odds rose 12 points (40% to 52%) over the last hour, on 280 contracts of net Yes buying, mostly from one wallet". For price markets it says whether the odds simply followed the perp, or moved on trading here while the fair odds barely changed.
- **Book depth in dollars:** what it takes to push Yes 5 points either way.

Clicking an event card opens the **event page**: the five likeliest outcomes' odds on one chart (12 hours or 2 weeks; point at it for everyone's odds at that moment), every outcome with its odds, bid / ask, 24h change, volume and contracts held, and the add-up check. Clicking a line (on a card, the chart's legend or the table) opens that market. A market's page also has the odds chart (12 hours or 2 weeks, with the fair-odds line for price markets), the Yes book (No is its mirror: 100 minus these), and the trades with the wallet behind each one.

These markets are small next to perps (often hundreds to a few thousand dollars a day), so Analytix doesn't look for perp-sized sweeps or cascades here; everything is scaled to the market. The fair odds are a simple estimate to compare against, not advice.

The data: `outcomeMeta` once a minute for the market list; a third WebSocket follows each market's Yes book and its market data (24h volume, price a day ago, contracts held), up to 300 markets (`ANALYTIX_OUTCOME_MARKETS`; Hyperliquid allows 1000 feeds per IP); a market's trades are followed live while its page is open (4 at a time, closed after 10 minutes unviewed). Live mode only (`engine/outcomes.py`, `api/outcomes.py`, `ingestion/outcomes.py`).

### Edges on prediction markets (independent of the order book)

Hyperliquid's prediction-market books are thin, and arbitrage bots keep them close to Polymarket and Kalshi, so those venues add little. Analytix compares each market with estimates that **don't come from any prediction-market book**:

| Source | For | How |
|---|---|---|
| **Options market** (Deribit) | price markets on BTC, ETH, SOL | the implied-volatility surface, interpolated in time (total variance) and across strikes; "above K at T" is priced as a digital option with the smile correction, P = N(d2) − F·φ(d1)·√T·∂σ/∂K, measured from Hyperliquid's own price; "touch" markets use the reflection rule |
| **The game** (ESPN) | matches (NFL, college football, NBA, MLB, NHL, the main soccer leagues and European cups) | the final score once a game ends; live, ESPN's own win model for American football, and for soccer a goals model (each side scores at a steady rate fitted to the pre-match line; only the goals still to come are random); before kick-off, the sportsbook line with the margin taken out |
| **Perp moves** | price markets with no options market (trade.xyz stocks and commodities, HYPE) | the perp's own volatility hour by hour of the week, scaled to the Cboe volatility index where there is one |

From that estimate and the prices you can actually trade (never a stale last trade):

- **Edge board** at the top of Outcomes: the best trades right now (+3% expected value or more, $20+ available): which side to buy, the price, the estimate and where it comes from, expected value per $1, a quarter-Kelly stake (full Kelly assumes the estimate is exact), what's available, and the return if right (and per year).
- **Cards and ladders** show the estimate as a tick on each odds bar, an "Edge:" line, LIVE/FINAL game chips, and the ladder's **implied distribution** (where the price ends, by band) next to the estimate's.
- **Sharp wallets.** Every trade on the 100 busiest markets is checked 15 minutes and 2 hours later. Wallets with 15+ trades, +1.5 points on average and a t-stat of 2+ are marked sharp (the mirror image: usually wrong). Cards show which way sharp wallets have traded in the last 6 hours; market pages list the known wallets. Learned live, kept across restarts.
- **Scorecard.** Every estimate is scored on markets that settle: at 24h, 6h and 1h before each close it saves the estimate, the market's odds and the best bid and ask, and once the market settles it compares them with what happened (Brier score: how much closer to the outcome each source was than Hyperliquid's own price), and what buying its +3% edges returned per $1. After 15 settled markets a source gets a verdict. **Edges right now** has a Record column: a source that has been less accurate than the market is hidden from the board and never alerts; our own models (perp moves, the goals model) are shown while unproven but don't alert until they've beaten the market; other markets' prices (options, sportsbooks, ESPN) count from the start. Sportsbook edges need 4 points more expected value to alert, and alerts name their source first and its record.
- **Calibration.** Each market's odds 24h, 6h and 1h before its close are noted and compared with how it ended, in 10-point bands, to measure biases like overpriced long shots on Hyperliquid's own markets. Shows once 30 markets have settled; kept across restarts.
- **News pulse** (GDELT): a chip only when coverage of a market's teams or names is 3× its own normal or more; the page shows the counts, keyword flags from the headlines (injury, suspended, leaves, legal, delay, deal) and 3 headline links. No summaries.
- **Rules** in plain words (what counts, the official source, when it settles).
- **Prediction market edge** alert (Telegram): a market trades at least N% expected value from the estimate with $50+ available (default 8%), once per market and side until it closes back below half that.

Sources refresh on their own (options every minute; scoreboards every 20 seconds while a game is on; one news query every 6 seconds, rotating). `/api/health` → `prediction_markets.sources` shows each one's state; `python -m scripts.probeIntel` on the server checks them all. Not advice: every estimate has its own error.

### Hold clock (very high leverage)

The **Hold clock** button answers "how long can I hold this?" at 100× to 1000× (built with Papertrade's 800× in mind, where every fill is Hyperliquid's mid and the bust triggers on the best bid or ask). At 800× the bust is about 0.12% away, so what matters is how often the best bid (for a long) or ask (for a short) actually reaches that far within seconds and minutes, wicks and spread blowouts included.

It keeps a one-second grid of each main coin's lowest best bid, highest best ask and last mid (24 hours, saved in the bars SQLite file), built from the books the dashboard already receives, and from it shows:

- **Survival by hold time** (10s, 30s, 1, 2, 5 and 15 minutes): the share of real starts over the last day where the bust wasn't reached in that time, overall and for **conditions like now** (starts whose last 5 minutes moved about as much as the last 5 minutes have). Headline: the hold time that survives 9 times in 10, and when half are gone. Feed gaps are left out, never filled in.
- **Fill drift**: how far the mid moves in the 1 to 2 seconds between clicking and the fill landing on-chain, as bps and as a share of margin at your leverage (typical, 1 in 10, worst 1%).
- **The race**: the chance the mid reaches your target (a % of margin) before the best bid/ask reaches your bust, how long the wins took, and what a win pays after Papertrade's cut, with the average result per trade.
- **What a win pays**: +10% to +500% targets at your leverage after Papertrade's cut on winning closes, exactly as their docs give it: the exit counts 0.2 bps worse, the asymmetric impact haircut (1 − baseRate) / (1 + 1/(move·rateMultiplier) + referenceNotional/(10⁶·move·positionMultiplier)) with the BTC and ETH launch settings, then a 2% win fee. Small moves lose the most, so quick scalps at high leverage keep a smaller share. Losses pay nothing extra.
- **The tape right now**: volatility against usual, the spread against usual, which way the book leans, and Binance's last few seconds against Hyperliquid's, with plain warnings when the spread is blown out, the book leans against you or Binance has moved your bust's way and Hyperliquid hasn't yet.
- **Your position**: enter your fill (and the bust price your venue shows) for P&L on margin, what closing now pays after Papertrade's cut, the distance to bust and survival odds from here.

Measured, not modelled; live mode only (`engine/holdclock.py`, `api/holdclock.py`, `GET /api/hold`).

The **Bot** button is a trading bot in **shadow mode**: it trades your rules on the live timeframe cards and fills every trade the way Hyperliquid itself would, but sends no orders. It answers "would this survive real costs?" before any money is involved.

- **Auto hunter** (on by default, 24/7): every second, on every watched coin, it looks for eight setups Analytix already recognises and trades each one the moment it appears (once per new opportunity, not every second it lasts): **Binance leads** (Binance moved at least 2.5 normal seconds' worth over 3s and Hyperliquid has made at most half of it), **Flow + Book** (both lean the same way on 1m), **Winning side** (mostly buyers or sellers on 1m and price moving their way, not soaked up, with the book not leaning against them), **Breakout** (a significant 1m move with flow behind it), **Cascade** (a new chain of sweeps or liquidations: follow it), **Cascade fade** (one just ended: trade the snap back), **Absorption** (heavy one-way flow on 1m or 10m that isn't moving price: trade against it) and **Level break** (a defended level gives way). Each setup runs on each coin with four exits (Quick +12/−8 bps within 90s, Standard +25/−15 within 5 min, Runner +60/−30 within 30 min, Swing +120/−50 within 2 h) and both order styles, except market orders where the target can't cover the fees (a break-even win rate over 75%, i.e. Quick): 56 strategies per coin. A strategy that is clearly losing (20+ trades, average below zero, t ≤ −1) is **benched** and stops trading; Unbench brings them back. Each setup shows its result before fees too, to tell a bad signal from one eaten by costs. When the auto hunter's design changes, its saved record starts fresh. The panel shows signals and results per setup, a leaderboard of the best and worst strategies after costs, the live signal feed and what's open; Telegram gets an hourly digest. Settings: coins, setups, exits, leverage (capped at each coin's max), margin, daily stop.

- **Rules**: pick a coin, a card (1m to 1w) and the rows that must agree (on 1m: Flow, Who, Forced, Book), how strong each must be, longs, shorts or both, leverage (up to Hyperliquid's max for the coin), margin, a target and stop in bps, a time limit, whether to close when the rows flip, a cooldown, a trades-per-hour cap and a daily loss stop. Up to 12 rules; changing a rule's trading settings starts a fresh track record.
- **Two ways at once**: every rule runs with **market orders** (fills half a second after the signal at the best ask or bid, walking the book for size, 0.045% a side) and with **limit orders** (joins the best bid or ask, counts as filled only when a trade prints through it, so it can miss; 0.015%, take-profits rest as limits). Stops, time exits and flips are market orders. Stops are checked on every book update, so a wick fills past the stop as it would for real. Funding is charged while open, and isolated liquidation (maintenance at half the initial margin at max leverage) caps a loss at the margin.
- **Track record** per rule and style: trades, wins, the win rate needed to break even after fees, before fees, fees, net, average per trade, worst dip, today, a plain verdict (too early before 30 trades; "the costs eat the edge" when it wins before fees and loses after; "steady enough that luck is an unlikely explanation" at t ≥ 2), what's open now, and the latest trades.
- **Telegram** (live mode, using the Alerts bot): off, an hourly summary, or every trade; a message when a daily loss stop trips.

Rules live in `data/bot.json`, closed trades in the bars SQLite file (`engine/bot.py`, `api/bot.py`, `GET /api/bot`, `POST /api/bot/rules`, `DELETE /api/bot/rules/{id}`, `POST /api/bot/rules/{id}/reset`). Live trading, through a Hyperliquid API wallet that can trade but not withdraw, comes next.

### Position watch

The **Positions** button opens position watch. Paste a wallet address (and a name if you like) and it shows that wallet's open Hyperliquid positions, refreshed every 20 seconds. It's read-only: Hyperliquid shows every wallet's positions publicly, so it needs no keys and can't trade. Up to 10 wallets; they're kept in `data/watch.json` on the machine running Analytix.

For each position:

- size, leverage (cross or isolated), entry, the price now, and P&L;
- **the chance of liquidation in the next hour and the next 24 hours**, from the same simulation as Plan a trade (5-minute candles for the hour, hourly candles for the day, volatility brought up to the moment);
- how far liquidation is, in % and in normal 24-hour moves;
- the stop-loss and take-profit on the book, if any, with the chance each one closes the position first in the next 24h, and a warning when the stop sits inside the normal daily swing;
- what funding costs (or pays) per day at today's rate.

The liquidation price is Hyperliquid's own, so it already accounts for cross margin and the other open positions. The odds hold it fixed; in cross margin it really moves as the other positions win or lose. The **Position risk** alert pings Telegram when a position's chance of liquidation in the next 24 hours passes your threshold, and again, more urgently, when the chance within the next hour does. Each re-arms once the chance falls back below half the threshold. Position watch runs in live mode only (`engine/positions.py`, `api/positions.py`, `ingestion/account.py`).

### Plan a trade

The **Plan a trade** button opens the position planner. Pick long or short, margin, leverage and how long you'll hold (15m … 1w). It answers, before you click buy:

- **Chance of liquidation before you exit** and the liquidation price (Hyperliquid's isolated-margin formula, maintenance = half the initial margin at the coin's max leverage).
- **Safe leverage**: the most leverage that keeps that chance under 1% / 5% for this hold.
- **Where price trades before you exit**: round levels above and below with the chance price touches each, so you can see where a stop would be hit by noise and where a target is realistic.
- **When you close**: the spread of P&L outcomes (from the worst 1 in 20 to the best 1 in 20) after fees, slippage on the live book and funding, and the chance of being in profit.
- **Costs**: fees (0.045% in and out), slippage now, and funding expected over the hold (today's rate drifting back to the week's average).
- **Isolated or cross margin.** In cross (Hyperliquid's default), your whole account balance backs the position: liquidation is where the account runs out, and a liquidation takes the account.
- **The price cone**: where half and 90% of simulated paths are at each moment of the hold, with entry, stop, target and liquidation drawn on it; hover for the numbers and the chance of liquidation by then.
- **Bad cases**: the worst 1-in-100 result, the average of the worst 5% (expected shortfall), and the chance of losing half your margin or more.
- **Slippage beyond the visible book**: sizes the 20 visible levels can't fill are costed with the square-root impact law (≈ 0.7 × daily volatility × √(size ÷ 24h volume)) instead of being left out.
- **Stop and take-profit** (optional, % from entry): the chance each closes the trade first, or that neither does before you exit; what each is worth after costs; how long each typically takes; and the average result. If one candle reaches both, the loss is taken as first. A stop past the liquidation price is flagged, since liquidation would come first. With no directional view the average is roughly minus the costs whatever the bracket: it changes the shape of your results, not the average.

How the odds are made: filtered historical simulation, the method risk desks use for VaR (`engine/planner.py`):

1. The coin's own candles from Hyperliquid: hourly (~200 days) for holds over 6h, 5-minute (~17 days) for shorter ones. Each candle gives three moves from the previous close: to the close, the low and the high (wicks are what liquidate people).
2. Time of day and weekends are taken out (measured on the ~200 days of hourly candles, reused for the 5-minute model).
3. A GARCH(1,1) gives each candle's expected volatility; dividing by it leaves the coin's shape of surprise (fat tails, lopsided wicks) without the regime. How long volatility lingers is uncertain, so each path draws its own GARCH parameters from how well they fit.
4. Volatility is brought up to the moment through every candle since the fit and the move so far, so a crash ten minutes ago counts.
5. 20,000 paths draw random historical candles scaled to the volatility expected at each step; each path's worst point, best point and exit are read off.

**Checked on the coin's own history, every time.** For each coin and hold, the planner refits itself on the older 60% of the candles and walks through the newer 40% it never saw: at up to 200 past moments it predicts the chance of price reaching levels ½ to 3 typical moves away before the hold ends, then checks the candles. The drawer shows what it said against what happened ("it said 5.5%, it happened 4.7%"), how often its 90% range held, and the same check for a plain bell-curve model. If the model is wrong for a coin, you see it there.

No direction is assumed: historical drift is removed, so it sizes the room a trade needs, not which way price goes. Checked in `tests/test_planner.py` against brute-force simulation of known processes (touch odds within ~15% at 4h–3d; a 1-week 1-in-100 tail within 3×, the limit of what 200 days of data can tell). Assumptions shown with every plan: isolated margin; liquidation costs the whole margin plus entry costs; candle wicks are last-trade prices while liquidation uses the mark price, so wicks slightly overstate the risk. In replay mode, or before the candles load, it falls back to a rough fat-tailed walk and says so.

### Does any of it work? Measure it

`scripts/study.py` replays recordings and, using only what the engine knew at each moment, logs every time one of these reads fires and the direction it implies. It also logs every card tag on the 1m, 10m and 60m cards that leans one way ("price held", "TWAP", "far above", "uptrend", "liquidations"…), in the direction the row leans. Then it checks the price 1, 5, 15 and 60 minutes later:

```bash
python -m scripts.record                 # leave it running: days, not hours
python -m scripts.study                  # every coin in data/recordings
python -m scripts.study data/recordings --coin ETH --min-n 30 --csv study.csv
```

```
14 session(s) · 212.5 hours · ETH, BTC, SOL, HYPE

signal                                     n  │ 1m  hit   avg    t  │ 5m  hit   avg    t  …
absorption (10m)                          41  │     58%  +2.1  +1.9 │     55%  +3.4  +1.2 …
card vwap: far above (10m)                88  │     44%  -0.9  -1.4 │     41%  -2.6  -2.2 …
```

`hit` is the share that went the implied way, `avg` the mean move that way in bps, `t` the average ÷ its standard error. A card tag with a negative `avg` means fading it worked (above: price far above VWAP tended to come back). Under ~30 occurrences or with |t| < 2, it isn't known yet. Keep the reads that hold up across days and coins, and drop the ones that don't. (The numbers above are illustrative. The synthetic session is far too short to say anything.)

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
- **The event log survives restarts in live mode** (the last week, saved to the same SQLite file as the minute bars), but explaining an arbitrary past moment still needs that moment's ticks in memory.
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
| `ANALYTIX_RECORD_FILE` | none | in live mode, also record raw messages to this one file |
| `ANALYTIX_LIQUIDATORS` | none | comma-separated addresses |
| `ANALYTIX_EXTERNAL` | `binance,bybit` | other exchanges to compare with for "where it started" (live mode); empty to turn off |
| `ANALYTIX_BINANCE_WS` / `ANALYTIX_BYBIT_WS` | their public futures feeds | override the exchange WebSocket addresses |
| `ANALYTIX_BACKFILL` | `1` | `0` to skip downloading price candles on start |
| `ANALYTIX_BARS_DB` | `data/bars.sqlite` | where live minute bars are saved (live mode only) |
| `ANALYTIX_OUTCOME_MARKETS` | `300` | how many prediction markets the Outcomes tab follows live (two feeds each) |

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
| `GET /api/outcomes` · `GET /api/outcomes/{id}?range=12h\|2w` · `GET /api/outcomes/questions/{id}?range=12h\|2w` | every prediction market with live odds, fair odds and the add-up check; one market's page (book, trades, who is trading, why the odds moved, odds history) |
| `GET /api/hold?coin=BTC&side=long&leverage=800&target_pct=50&entry=&bust=` | the hold clock: survival odds by hold time (overall and like now), fill drift, the target-vs-bust race, the tape right now, and a live position read |
| `WS /ws?coin=BTC` | a snapshot on connect, then 2× per second; send `{"coin": "SOL"}` to switch |

`coin` defaults to `ANALYTIX_COIN` everywhere.

## First live run checklist

1. `python -m uvicorn api.app:app`. The badge should say **Live** within a few seconds.
2. `/api/health`: `messages` should be climbing and `engine_runs` should tick once per second.
3. The 10m card fills after 10 minutes and the 60m card after an hour. The 6h … 1w cards show price immediately (backfilled) and their flow data fills in as the app runs. Until then the cards say "warming up".
4. Record a session (`python -m scripts.record --minutes 30`) and run `python -m scripts.replayReport <file> --coin ETH`. Check whether the explanations match what you saw.
5. Tune `config.py`. The sweep floor (`MIN_SWEEP_NOTIONAL`) and `SIGNIFICANT_Z` are the first knobs to adjust against real ETH flow.

## Roadmap

- Prediction markets, next: position watch for outcome positions; an Elo model for tournament winners
- Track how often the fair odds beat the market's odds once markets settle
- A "what happened at 14:32?" query over the saved history
- More windows: add them to `WINDOWS` in `config.py`; anything over an hour automatically uses minute bars
- Per-coin thresholds (a $50K sweep floor is large for SOL, small for BTC)

"""
Single source of truth for every tunable in Analytix.

Rule: if a number changes behaviour, it lives here and everything else imports
it. Add a timeframe to WINDOWS and the buffers, engine, API and frontend all
pick it up — nothing else needs editing.
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# ── Market ──────────────────────────────────────────────────────────────────
# Hyperliquid coin names are case-sensitive (e.g. "kPEPE"), so no .upper().
COINS: list[str] = [c.strip() for c in os.getenv("ANALYTIX_COINS", "ETH,BTC,SOL,HYPE").split(",") if c.strip()]
COIN = os.getenv("ANALYTIX_COIN", COINS[0] if COINS else "ETH")   # selected by default
if COIN not in COINS:
    COINS.insert(0, COIN)
HL_WS_URL = os.getenv("ANALYTIX_WS_URL", "wss://api.hyperliquid.xyz/ws")

# ── Timeframes ──────────────────────────────────────────────────────────────
WINDOWS: dict[str, int] = {
    "1m": 60,
    "10m": 600,
    "60m": 3600,
}
MAX_WINDOW_S = max(WINDOWS.values())

# History buffers keep the longest window plus a margin, so the longest window can
# still find the book / funding state *as of* its start.
BUFFER_MARGIN_S = 30
HISTORY_SECONDS = MAX_WINDOW_S + BUFFER_MARGIN_S

# ── Cadence ─────────────────────────────────────────────────────────────────
ANALYSIS_INTERVAL_MS = 1_000   # engine runs once per *exchange* second
# Long windows barely change second to second, so each window is recomputed at
# most every seconds/WINDOW_REFRESH_DIVISOR: 1m every 1s, 10m every ~2s, 60m every 10s.
WINDOW_REFRESH_DIVISOR = 360
CHART_POINTS = 900             # the price chart is downsampled to about this many points
BROADCAST_INTERVAL_S = 0.5     # API pushes to browsers twice per *wall* second
STALE_AFTER_S = 5.0            # ingestion flags the feed stale after this much silence
PING_INTERVAL_S = 20.0         # Hyperliquid drops idle sockets after ~60s

# ── Order book ──────────────────────────────────────────────────────────────
BOOK_LEVELS_SHOWN = 12         # levels per side sent to the frontend
DEPTH_BAND_BPS = 25.0          # "near-touch" depth = liquidity within this band of mid

# ── Move significance ───────────────────────────────────────────────────────
# A move is judged against the market's own recent volatility (a z-score),
# with an absolute floor so a dead market doesn't flag 1-tick wiggles.
DEFAULT_SIGMA_1S_BPS = 1.0     # fallback until enough history exists
MIN_SIGMA_SAMPLES = 60
MIN_NOTABLE_BPS_PER_SQRT_MIN = 3.0
NOTABLE_Z = 1.5
SIGNIFICANT_Z = 2.5

# ── Signals ─────────────────────────────────────────────────────────────────
MIN_DRIVER_STRENGTH = 0.25     # below this a signal is noise, not a driver
IMBALANCE_FULL_STRENGTH = 0.4  # (buy-sell)/(buy+sell) at which flow strength saturates
DEPTH_FULL_STRENGTH = 0.6      # relative depth shift at which depth strength saturates
OI_REF_PCT_PER_SQRT_MIN = 0.3  # OI change that counts as "full strength" for a 1m window
CROWDED_FUNDING_APR = 20.0     # |funding| above this (annualised %) = crowded side

# Sweeps / liquidations (heuristic — see signals/liquidations.py)
MIN_SWEEP_NOTIONAL = 50_000    # USD floor for a single aggressive order to count as a sweep
SWEEP_NOTIONAL_PCTL = 0.97     # ...and it must be in the top 3% of recent orders
SWEEP_MIN_LEVELS = 3           # ...or walk at least this many price levels
CASCADE_MIN_SWEEPS = 3         # same-side sweeps chained together = cascade
CASCADE_MAX_GAP_MS = 4_000
# Optional: known liquidator addresses (comma separated). Trades touching them
# are flagged as confirmed liquidations instead of "liquidation-like".
LIQUIDATOR_ADDRESSES = {
    a.strip().lower()
    for a in os.getenv("ANALYTIX_LIQUIDATORS", "").split(",")
    if a.strip()
}

# ── Event log ───────────────────────────────────────────────────────────────
EVENT_LOG_SIZE = 200

# ── Runtime mode ────────────────────────────────────────────────────────────
MODE = os.getenv("ANALYTIX_MODE", "live")            # "live" | "replay"
REPLAY_FILE = Path(os.getenv("ANALYTIX_REPLAY_FILE", ROOT / "data" / "sampleSession.jsonl"))
REPLAY_SPEED = float(os.getenv("ANALYTIX_REPLAY_SPEED", "4"))
REPLAY_LOOP = os.getenv("ANALYTIX_REPLAY_LOOP", "1") == "1"
RECORD_FILE = os.getenv("ANALYTIX_RECORD_FILE")      # live mode: also record raw messages here
FRONTEND_DIST = ROOT / "frontend" / "dist"

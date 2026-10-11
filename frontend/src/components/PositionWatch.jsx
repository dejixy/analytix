import { useCallback, useEffect, useRef, useState } from "react";
import { fmtPrice } from "../format.js";
import { chance, pct, riskBand, usd } from "./Planner.jsx";

const shortAddr = (a) => `${a.slice(0, 6)}…${a.slice(-4)}`;
const ago = (ms) => {
  const s = Math.max(0, Math.round((Date.now() - ms) / 1000));
  return s < 90 ? `${s}s ago` : `${Math.round(s / 60)}m ago`;
};

/** One open position: size and P&L, the chance of liquidation soon, and what its stop and take-profit look like. */
function PositionCard({ p }) {
  const r = p.risk;
  const long = p.side > 0;
  const [band, cls] = r ? riskBand(r.p_liq_24h) : ["", ""];
  return (
    <div className="pw-pos">
      <div className="pw-pos-head">
        <span>
          <b>{p.coin}</b> <span className={long ? "up" : "down"}>{long ? "Long ▲" : "Short ▼"}</span>
          <span className="muted"> · {p.leverage}× {p.cross ? "cross" : "isolated"} · {usd(p.value)}</span>
        </span>
        <span className={`num ${p.pnl >= 0 ? "up" : "down"}`}>
          {usd(p.pnl, true)} <span className="muted">({pct(p.roe * 100)})</span>
        </span>
      </div>
      {r ? (
        <>
          <div className="pw-odds">
            <div>
              <div className="plan-kicker">Liquidation, next hour</div>
              <div className={`pw-odd ${riskBand(r.p_liq_1h)[1]}`}>{chance(r.p_liq_1h)}</div>
            </div>
            <div>
              <div className="plan-kicker">Liquidation, next 24h</div>
              <div className={`pw-odd ${cls}`}>
                {chance(r.p_liq_24h)} <span className={`plan-band ${cls}`}>{band}</span>
              </div>
            </div>
          </div>
          <div className="pw-line">
            {p.liq_price ? (
              <>
                Liquidation at <b>{fmtPrice(p.liq_price)}</b>, {Math.abs(r.liq_pct).toFixed(1)}% {long ? "below" : "above"} the
                price now{r.liq_moves != null && ` (${r.liq_moves.toFixed(1)}× a normal day's move)`}.
              </>
            ) : (
              "No liquidation price: at today's prices this position can't be liquidated."
            )}
          </div>
          <div className="pw-line muted">
            Entry {fmtPrice(p.entry)} · now {fmtPrice(p.mark)} · a normal day moves about ±{r.day_move_pct.toFixed(1)}%
          </div>
          {p.stop != null && (
            <div className="pw-line">
              Stop at <b>{fmtPrice(p.stop)}</b>:{" "}
              {r.stop_beyond_liq ? (
                "it's past your liquidation price, so liquidation would close the position first."
              ) : (
                <>
                  <b>{chance(r.p_stop_24h)}</b> chance it closes the position in the next 24h
                  {r.p_stop_24h >= 0.5 ? ". It sits inside the normal daily swing, so noise alone will likely hit it." : "."}
                </>
              )}
            </div>
          )}
          {p.target != null && (
            <div className="pw-line">
              Take-profit at <b>{fmtPrice(p.target)}</b>: <b>{chance(r.p_target_24h)}</b> chance it's hit first in the next 24h.
            </div>
          )}
          {p.stop == null && p.target == null && <div className="pw-line muted">No stop-loss or take-profit on the book.</div>}
          {r.funding_day != null && Math.abs(r.funding_day) >= 0.005 && (
            <div className="pw-line">
              Funding: you {r.funding_day >= 0 ? "pay" : "earn"} about <b>{usd(Math.abs(r.funding_day))}</b> a day at today's rate.
            </div>
          )}
        </>
      ) : (
        <div className="pw-line muted">{p.risk_error || "Working out the odds…"}</div>
      )}
    </div>
  );
}

export default function PositionWatch({ onClose }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [addr, setAddr] = useState("");
  const [label, setLabel] = useState("");
  const alive = useRef(true);
  const close = useRef(onClose);
  close.current = onClose;

  const call = useCallback(async (method, path = "", body) => {
    try {
      const r = await fetch(`/api/positions${path}`, {
        method, headers: body ? { "Content-Type": "application/json" } : undefined,
        body: body ? JSON.stringify(body) : undefined,
      });
      const d = await r.json();
      if (!alive.current) return false;
      if (!r.ok) {
        setError(typeof d.detail === "string" ? d.detail : "That didn't work.");
        return false;
      }
      setError(null);
      setData(d);
      return true;
    } catch {
      if (alive.current) setError("Couldn't reach the backend.");
      return false;
    }
  }, []);

  useEffect(() => {
    alive.current = true;
    call("GET");
    const t = setInterval(() => call("GET"), 5_000);
    const onKey = (e) => e.key === "Escape" && close.current();
    window.addEventListener("keydown", onKey);
    return () => {
      alive.current = false;
      clearInterval(t);
      window.removeEventListener("keydown", onKey);
    };
  }, [call]);

  const add = async () => {
    setBusy(true);
    const ok = await call("POST", "/wallets", { address: addr.trim(), label: label.trim() });
    if (alive.current) setBusy(false);
    if (ok) {
      setAddr("");
      setLabel("");
    }
  };

  return (
    <div className="planner-scrim" onClick={onClose}>
      <aside className="planner" role="dialog" aria-label="Position watch" onClick={(e) => e.stopPropagation()}>
        <div className="panel-head">
          <h2 className="panel-title">Position watch</h2>
          <button className="plan-close" onClick={onClose} aria-label="Close">✕</button>
        </div>

        {error && <div className="plan-error">{error}</div>}
        {!data ? (
          <p className="plan-note muted" style={{ padding: 12 }}>Loading…</p>
        ) : (
          <>
            <section className="plan-sec">
              <h3>Watch a wallet</h3>
              {!data.enabled && (
                <p className="plan-note">Position watch reads wallets from Hyperliquid, so it only runs in live mode.</p>
              )}
              <div className="alert-row">
                <span className="plan-input">
                  <input className="left" placeholder="0x… wallet address" value={addr} spellCheck={false}
                    onChange={(e) => setAddr(e.target.value)} onKeyDown={(e) => e.key === "Enter" && add()}
                    aria-label="Wallet address" />
                </span>
                <span className="plan-input pw-name">
                  <input className="left" placeholder="name (optional)" value={label} maxLength={40}
                    onChange={(e) => setLabel(e.target.value)} onKeyDown={(e) => e.key === "Enter" && add()}
                    aria-label="Name for this wallet" />
                </span>
                <button className="plan-chip" disabled={busy || !addr.trim()} onClick={add}>Add</button>
              </div>
              <p className="plan-note muted">
                Read-only. Hyperliquid shows every wallet's positions publicly, so this only needs the address: no
                keys, and it can't trade. Checked every {data.poll_s}s.
              </p>
            </section>

            {data.wallets.map((w) => {
              const acct = w.account;
              return (
                <section className="plan-sec" key={w.address}>
                  <div className="pw-wallet-head">
                    <h3>{w.label || shortAddr(w.address)}</h3>
                    <button className="toast-x" aria-label="Stop watching this wallet" title="Stop watching"
                      onClick={() => call("DELETE", `/wallets/${w.address}`)}>×</button>
                  </div>
                  <div className="pw-sub muted num">
                    {w.label ? `${shortAddr(w.address)} · ` : ""}
                    {acct ? `account ${usd(acct.value)} · margin used ${usd(acct.margin_used)}` : ""}
                    {w.updated_ms ? ` · updated ${ago(w.updated_ms)}` : ""}
                  </div>
                  {w.error && <p className="plan-note">{w.error}</p>}
                  {!acct && !w.error && (
                    <p className="plan-note muted">{data.enabled ? "Reading this wallet…" : "Waiting for live mode."}</p>
                  )}
                  {acct && acct.positions.length === 0 && <p className="plan-note muted">No open positions.</p>}
                  {acct?.positions.map((p) => <PositionCard key={`${p.coin}${p.side}`} p={p} />)}
                </section>
              );
            })}

            <footer className="plan-foot muted">
              Odds come from the same model as Plan a trade: each coin's own candles, with volatility brought up to
              the moment, and no view on direction. The liquidation price is Hyperliquid's own and is held fixed; in
              cross margin it moves as your other positions win or lose. Switch on <b>Position risk</b> in Alerts to
              get a Telegram ping when the chance of liquidation passes your threshold.
            </footer>
          </>
        )}
      </aside>
    </div>
  );
}

import { useCallback, useEffect, useRef, useState } from "react";
import { fmtPrice, fmtTime } from "../format.js";

const RULES = [
  { kind: "cascade", label: "Liquidation cascade", help: "A chain of forced selling or buying, once open interest confirms it", field: "min_usd", unit: "$", hint: "min liquidated" },
  { kind: "liquidation", label: "Big liquidation", help: "One account force-closed by the exchange", field: "min_usd", unit: "$", hint: "min size" },
  { kind: "twap", label: "New TWAP", help: "A wallet starts slicing a big order over time", field: "min_usd_per_hour", unit: "$", hint: "min per hour" },
  { kind: "level", label: "Level break", help: "A level that absorbed flow for minutes gives way" },
  { kind: "absorbed", label: "Absorbed flow", help: "Heavy one-sided flow fails to move price on the 10m or 60m card" },
  { kind: "volatility", label: "Volatility spike", help: "The 10m range is this many times its usual size", field: "min_ratio", unit: "×", hint: "times usual" },
  { kind: "price", label: "Price alerts", help: "Your levels below; each fires once when price crosses it" },
];
const NOTIFY_KEY = "analytix.notify";

const short = (x) => (x >= 1e6 ? `${+(x / 1e6).toFixed(2)}M` : x >= 1e3 ? `${+(x / 1e3).toFixed(1)}K` : `${x}`);
const parseUsd = (s) => {
  const m = String(s).trim().toUpperCase().replace(/[$,\s]/g, "").match(/^([\d.]+)([KMB]?)$/);
  if (!m) return null;
  return Number(m[1]) * ({ K: 1e3, M: 1e6, B: 1e9 }[m[2]] || 1);
};

export function notifyEnabled() {
  try {
    return localStorage.getItem(NOTIFY_KEY) === "1";
  } catch {
    return false;
  }
}

function RuleRow({ rule, cfg, onChange }) {
  const r = cfg.rules[rule.kind] || {};
  const [draft, setDraft] = useState(null);
  const value = rule.field ? r[rule.field] : null;
  const commit = () => {
    if (draft == null) return;
    const v = rule.unit === "$" ? parseUsd(draft) : Number(draft);
    setDraft(null);
    if (v != null && !Number.isNaN(v) && v !== value) onChange({ rules: { [rule.kind]: { [rule.field]: v } } });
  };
  return (
    <div className={`alert-rule ${r.on ? "" : "off"}`}>
      <button className={`plan-chip ${r.on ? "on" : ""}`} aria-pressed={!!r.on}
        onClick={() => onChange({ rules: { [rule.kind]: { on: !r.on } } })}>
        {r.on ? "On" : "Off"}
      </button>
      <div className="alert-rule-text">
        <div>{rule.label}</div>
        <div className="muted">{rule.help}</div>
      </div>
      {rule.field && (
        <label className="plan-input small alert-min" title={rule.hint}>
          {rule.unit === "$" ? "$" : ""}
          <input
            value={draft ?? (rule.unit === "$" ? short(value) : value)}
            onChange={(e) => setDraft(e.target.value)}
            onBlur={commit}
            onKeyDown={(e) => e.key === "Enter" && e.currentTarget.blur()}
            aria-label={`${rule.label}: ${rule.hint}`}
          />
          {rule.unit === "×" ? "×" : ""}
        </label>
      )}
    </div>
  );
}

export default function AlertsPanel({ coin, onClose }) {
  const [cfg, setCfg] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState("");
  const [token, setToken] = useState("");
  const [lvCoin, setLvCoin] = useState(coin);
  const [lvPrice, setLvPrice] = useState("");
  const [notify, setNotify] = useState(notifyEnabled());
  const alive = useRef(true);

  const call = useCallback(async (method, path, body, label = "") => {
    setBusy(label);
    try {
      const r = await fetch(`/api/alerts${path}`, {
        method, headers: body ? { "Content-Type": "application/json" } : undefined,
        body: body ? JSON.stringify(body) : undefined,
      });
      const d = await r.json();
      if (!alive.current) return null;
      if (!r.ok) {
        setError(typeof d.detail === "string" ? d.detail : "That didn't work.");
        return null;
      }
      setError(null);
      setCfg(d);
      return d;
    } catch {
      if (alive.current) setError("Couldn't reach the backend.");
      return null;
    } finally {
      if (alive.current) setBusy("");
    }
  }, []);

  useEffect(() => {
    alive.current = true;
    call("GET", "");
    const t = setInterval(() => call("GET", ""), 10_000);
    const onKey = (e) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => {
      alive.current = false;
      clearInterval(t);
      window.removeEventListener("keydown", onKey);
    };
  }, [call, onClose]);

  const update = (patch) => call("PUT", "", patch);
  const tg = cfg?.telegram;

  const toggleNotify = async () => {
    if (notify) {
      try { localStorage.setItem(NOTIFY_KEY, "0"); } catch { /* private mode */ }
      setNotify(false);
      return;
    }
    if (!("Notification" in window)) {
      setError("This browser doesn't support notifications.");
      return;
    }
    const perm = Notification.permission === "granted" ? "granted" : await Notification.requestPermission();
    if (perm !== "granted") {
      setError("Notifications are blocked for this site — allow them in the browser's site settings.");
      return;
    }
    try { localStorage.setItem(NOTIFY_KEY, "1"); } catch { /* private mode */ }
    setNotify(true);
  };

  return (
    <div className="planner-scrim" onClick={onClose}>
      <aside className="planner" role="dialog" aria-label="Alerts" onClick={(e) => e.stopPropagation()}>
        <div className="panel-head">
          <h2 className="panel-title">Alerts</h2>
          <button className="plan-close" onClick={onClose} aria-label="Close">✕</button>
        </div>
        {error && <div className="plan-error">{error}</div>}
        {!cfg ? (
          <p className="plan-note muted" style={{ padding: 12 }}>Loading…</p>
        ) : (
          <div className="plan-body">
            <section className="plan-sec">
              <h3>Latest</h3>
              {cfg.recent.length === 0 ? (
                <p className="plan-note muted">Nothing yet. Alerts start five minutes after Analytix starts, once it knows what's normal.</p>
              ) : (
                <ul className="alert-feed">
                  {cfg.recent.map((a) => (
                    <li key={a.id}>
                      <div><span className={`alert-dot ${a.direction}`} /> <b>{a.title}</b></div>
                      <div className="muted">{fmtTime(a.ts)}{a.lines.length ? ` · ${a.lines[0]}` : ""}</div>
                    </li>
                  ))}
                </ul>
              )}
            </section>

            <section className="plan-sec">
              <h3>Telegram</h3>
              {!tg.enabled && <p className="plan-note">Telegram only sends in live mode (a replay can't ping your phone).</p>}
              {!tg.connected ? (
                <>
                  <ol className="alert-steps">
                    <li>In Telegram, message <b>@BotFather</b>, send <code>/newbot</code>, pick a name.</li>
                    <li>Paste the token it gives you here (it stays on this machine and is never shown again).</li>
                    <li>Open your new bot and press <b>Start</b>, then come back and press <b>Find chats</b>.</li>
                  </ol>
                  <div className="alert-row">
                    <span className="plan-input">
                      <input type="password" className="left" placeholder="123456:ABC…" value={token} onChange={(e) => setToken(e.target.value)} aria-label="Bot token" />
                    </span>
                    <button className="plan-open" disabled={!token || busy} onClick={async () => {
                      if (await call("POST", "/telegram/token", { token }, "token")) setToken("");
                    }}>{busy === "token" ? "Checking…" : "Connect"}</button>
                  </div>
                </>
              ) : (
                <>
                  <p className="plan-note">
                    Connected to <b>{tg.bot}</b>. Only the chats switched on below get alerts.
                    {tg.sent ? ` ${tg.sent} sent so far.` : ""}
                    {tg.last_error && <span className="plan-warn"> Last send failed: {tg.last_error}</span>}
                  </p>
                  {tg.chats.length === 0 ? (
                    <p className="plan-note muted">No chats yet: open {tg.bot} in Telegram, press Start (or add it to a group or channel), then Find chats.</p>
                  ) : (
                    <div className="alert-chats">
                      {tg.chats.map((c) => (
                        <div key={c.id} className="alert-rule">
                          <button className={`plan-chip ${c.on ? "on" : ""}`} aria-pressed={c.on}
                            onClick={() => call("PUT", `/telegram/chats/${c.id}`, { on: !c.on })}>{c.on ? "On" : "Off"}</button>
                          <div className="alert-rule-text"><div>{c.title}</div><div className="muted">{c.type}</div></div>
                        </div>
                      ))}
                    </div>
                  )}
                  <div className="alert-row">
                    <button className="plan-chip" disabled={!!busy} onClick={() => call("POST", "/telegram/discover", null, "find")}>
                      {busy === "find" ? "Looking…" : "Find chats"}
                    </button>
                    <button className="plan-chip" disabled={!!busy || !tg.chats.some((c) => c.on)} onClick={() => call("POST", "/telegram/test", null, "test")}>
                      {busy === "test" ? "Sending…" : "Send test"}
                    </button>
                    <button className="plan-chip" disabled={!!busy} onClick={() => call("DELETE", "/telegram")}>Disconnect</button>
                  </div>
                </>
              )}
            </section>

            <section className="plan-sec">
              <h3>This browser</h3>
              <div className="alert-rule">
                <button className={`plan-chip ${notify ? "on" : ""}`} aria-pressed={notify} onClick={toggleNotify}>{notify ? "On" : "Off"}</button>
                <div className="alert-rule-text">
                  <div>Desktop notifications</div>
                  <div className="muted">When the dashboard is open in a background tab</div>
                </div>
              </div>
            </section>

            <section className="plan-sec">
              <h3>What to alert on</h3>
              {RULES.map((r) => <RuleRow key={r.kind} rule={r} cfg={cfg} onChange={update} />)}
              <div className="alert-row">
                <span className="muted">Coins</span>
                <div className="plan-holds">
                  {cfg.watching.map((c) => {
                    const on = cfg.coins.length === 0 || cfg.coins.includes(c);
                    return (
                      <button key={c} className={`plan-chip ${on ? "on" : ""}`} aria-pressed={on} onClick={() => {
                        const current = cfg.coins.length ? cfg.coins : cfg.watching;
                        const next = on ? current.filter((x) => x !== c) : [...current, c];
                        update({ coins: next.length === cfg.watching.length ? [] : next });
                      }}>{c}</button>
                    );
                  })}
                </div>
              </div>
              <div className="alert-row">
                <span className="muted">Quiet time per alert</span>
                <label className="plan-input small">
                  <input type="number" min="1" max="1440" defaultValue={cfg.cooldown_min} key={cfg.cooldown_min}
                    onBlur={(e) => Number(e.target.value) !== cfg.cooldown_min && update({ cooldown_min: Number(e.target.value) })} />
                  min
                </label>
              </div>
            </section>

            <section className="plan-sec">
              <h3>Price alerts</h3>
              <div className="alert-row">
                <select className="plan-input small" value={lvCoin} onChange={(e) => setLvCoin(e.target.value)} aria-label="Coin">
                  {cfg.watching.map((c) => <option key={c} value={c}>{c}</option>)}
                </select>
                <span className="plan-input">
                  <input type="number" placeholder="price" value={lvPrice} onChange={(e) => setLvPrice(e.target.value)} aria-label="Price" />
                </span>
                <button className="plan-open" disabled={!(Number(lvPrice) > 0) || !!busy} onClick={async () => {
                  if (await call("POST", "/levels", { coin: lvCoin, price: Number(lvPrice) })) setLvPrice("");
                }}>Add</button>
              </div>
              {cfg.price_levels.length > 0 && (
                <ul className="alert-feed">
                  {cfg.price_levels.map((lv) => (
                    <li key={lv.id} className="alert-level">
                      <span><b>{lv.coin} {fmtPrice(lv.price)}</b>{lv.triggered_ms ? <span className="muted"> · crossed {fmtTime(lv.triggered_ms)}</span> : <span className="muted"> · waiting</span>}</span>
                      <button className="toast-x" aria-label="Remove" onClick={() => call("DELETE", `/levels/${lv.id}`)}>×</button>
                    </li>
                  ))}
                </ul>
              )}
            </section>

            <footer className="plan-foot muted">
              Alerts run where Analytix runs (your server), every second, for every coin. Each alert type has a quiet time per
              coin so a messy hour can't flood you. Until the study has a few weeks of data, alerts are events worth a look,
              not proven signals.
            </footer>
          </div>
        )}
      </aside>
    </div>
  );
}

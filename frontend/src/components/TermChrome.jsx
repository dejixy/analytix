import { useEffect, useRef, useState } from "react";

/* The terminal's working chrome: a command line, world market clocks, a log of what changed while you watched, and a
   status bar with the health of every data source. All of it does a job; none of it is decoration only. */

const pad2 = (n) => String(n).padStart(2, "0");

// ── commands ───────────────────────────────────────────────────────────────
export const COMMANDS = [
  { keys: ["EDGES", "E"], label: "Edges tab", tab: "edges" },
  { keys: ["NEWS", "N"], label: "News tab: calendar and headlines", tab: "news" },
  { keys: ["BOT", "B"], label: "Bot tab: paper trading", tab: "bot" },
  { keys: ["WRLD", "MAP", "WORLD", "CLK", "CLOCK"], label: "World map: hubs, night, flow, events, market clock", panel: "WRLD" },
  { keys: ["CHRT", "CHART", "CHARTS", "MON"], label: "Chart wall: every market, hourly", panel: "CHRT" },
  { keys: ["HEAT", "HOURS"], label: "When markets move: hour-of-day grid", panel: "FLOW", view: "hours" },
  { keys: ["LOG", "HEADLINES"], label: "What changed while you watched, and the headlines", panel: "LOG" },
  { keys: ["FLOW", "RIVER", "TRADES", "VOL"], label: "Flow river: dollars bought and sold, every market", panel: "FLOW", view: "river" },
  { keys: ["CNST", "LINKS", "ARCS"], label: "Market links: who moves together, and how that changed", panel: "CNST" },
  { keys: ["PRES", "ROSE", "PRESSURE"], label: "Pressure rose: buying and selling vs normal", panel: "PRES" },
  { keys: ["CORR", "GRID", "COMP"], label: "Every pair's link this week, as a grid", panel: "FLOW", view: "links" },
  { keys: ["CMDY", "COMMODITIES"], label: "Commodities table", panel: "CMDY" },
  { keys: ["WKND", "WEEKEND"], label: "Weekend record", panel: "WKND" },
  { keys: ["ROLL"], label: "Oil roll", panel: "ROLL" },
  { keys: ["FX", "CURRENCIES"], label: "Currencies", panel: "FX" },
  { keys: ["CAL", "CALENDAR"], label: "Coming up, 7 days", panel: "CAL" },
  { keys: ["EDGE", "BOARD"], label: "Edge board", panel: "EDGE" },
  { keys: ["WTI", "CL", "OIL"], label: "WTI oil", panel: "CMDY", mk: "CL" },
  { keys: ["BRENT", "BRENTOIL"], label: "Brent oil", panel: "CMDY", mk: "BRENTOIL" },
  { keys: ["GOLD", "XAU"], label: "Gold", panel: "CMDY", mk: "GOLD" },
  { keys: ["SILVER", "XAG"], label: "Silver", panel: "CMDY", mk: "SILVER" },
  { keys: ["JPY", "USDJPY", "YEN"], label: "USD/JPY", panel: "FX", mk: "JPY" },
  { keys: ["EUR", "EURUSD"], label: "EUR/USD", panel: "FX", mk: "EUR" },
  { keys: ["DXY", "DOLLAR"], label: "Dollar index", panel: "FX", mk: "DXY" },
  { keys: ["DASH", "DASHBOARD"], label: "Back to the order-flow dashboard", href: "/" },
  { keys: ["HELP", "?"], label: "List every command", help: true },
];

function find(q) {
  const u = q.trim().toUpperCase();
  if (!u) return [];
  if (u === "HELP" || u === "?") return COMMANDS.filter((c) => !c.help);
  const exact = COMMANDS.filter((c) => c.keys.includes(u));
  const rest = COMMANDS.filter((c) => !exact.includes(c) &&
    (c.keys.some((k) => k.startsWith(u)) || c.label.toUpperCase().includes(u)));
  return [...exact, ...rest].slice(0, 8);
}

/* Type a ticker or a panel code and press Enter (or GO). "/" or Ctrl+K from anywhere puts the cursor here. */
export function CommandBar({ onRun }) {
  const [q, setQ] = useState("");
  const [sel, setSel] = useState(0);
  const [msg, setMsg] = useState(null);
  const [focused, setFocused] = useState(false);
  const ref = useRef(null);

  useEffect(() => {
    const onKey = (e) => {
      const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName);
      if ((e.key === "/" && !typing) || (e.key.toLowerCase() === "k" && (e.ctrlKey || e.metaKey))) {
        e.preventDefault();
        ref.current?.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const list = find(q);
  const help = ["HELP", "?"].includes(q.trim().toUpperCase());
  const go = (c) => {
    if (!c || c.help) {
      if (!c && q.trim()) setMsg(`${q.trim().toUpperCase()}: no such command. Type HELP.`);
      if (c?.help) setQ("HELP");
      return;
    }
    onRun(c);
    setQ("");
    setMsg(null);
    setSel(0);
    ref.current?.blur();
  };
  const onKey = (e) => {
    if (e.key === "ArrowDown") { e.preventDefault(); setSel((s) => Math.min(s + 1, list.length - 1)); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setSel((s) => Math.max(s - 1, 0)); }
    else if (e.key === "Enter") { e.preventDefault(); go(help ? list[sel] : list[sel] || null); }
    else if (e.key === "Escape") { setQ(""); setMsg(null); ref.current?.blur(); }
  };

  return (
    <div className="term-cmd">
      <span className="term-cmd-prompt" aria-hidden="true">ANX ▸</span>
      <div className="term-cmd-box">
        <input ref={ref} value={q} spellCheck={false} autoComplete="off" aria-label="Command"
          placeholder="GOLD, WTI, ROLL, WKND, NEWS, HELP…   ( / to type )"
          onChange={(e) => { setQ(e.target.value); setSel(0); setMsg(null); }} onKeyDown={onKey}
          onFocus={() => setFocused(true)}
          onBlur={() => { setFocused(false); setTimeout(() => setMsg(null), 2500); }} />
        {((focused && list.length > 0) || msg) && (
          <div className="term-cmd-list" role="listbox">
            {msg && <div className="term-cmd-msg">{msg}</div>}
            {focused && list.map((c, i) => (
              <button key={c.keys[0]} role="option" aria-selected={i === sel} className={i === sel ? "on" : ""}
                onMouseDown={(e) => { e.preventDefault(); go(c); }}>
                <b>{c.keys[0]}</b><span>{c.label}</span>
              </button>
            ))}
          </div>
        )}
      </div>
      <button className="term-go" onClick={() => go(list[sel] || null)} aria-label="Run the command">GO</button>
    </div>
  );
}

// ── world clocks ──────────────────────────────────────────────────────────
const fmtCache = {};
function parts(now, tz) {
  fmtCache[tz] ||= new Intl.DateTimeFormat("en-GB", { timeZone: tz, weekday: "short", hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
  const p = Object.fromEntries(fmtCache[tz].formatToParts(new Date(now)).map((x) => [x.type, x.value]));
  return { wd: p.weekday, h: Number(p.hour), m: Number(p.minute), text: `${p.hour}:${p.minute}` };
}
const inHours = (p, spans) => !["Sat", "Sun"].includes(p.wd) && spans.some(([a, b]) => p.h * 60 + p.m >= a && p.h * 60 + p.m < b);
// Cash-market hours (holidays not modelled): when each city's market is trading
const CITIES = [
  { code: "NY", tz: "America/New_York", what: "NYSE", spans: [[570, 960]] },
  { code: "LDN", tz: "Europe/London", what: "LSE", spans: [[480, 990]] },
  { code: "TYO", tz: "Asia/Tokyo", what: "TSE", spans: [[540, 690], [750, 930]] },
];

export function WorldClocks({ now, cme }) {
  const you = Intl.DateTimeFormat().resolvedOptions().timeZone;
  return (
    <div className="term-clocks" aria-label="World markets">
      <span className={`term-city ${cme ? "on" : ""}`} title="CME and ICE: oil, gold and silver futures (Hyperliquid's references)">
        <i />CME/ICE <b>{cme == null ? "-" : cme ? "open" : "shut"}</b>
      </span>
      {CITIES.map((c) => {
        const p = parts(now, c.tz);
        const on = inHours(p, c.spans);
        return (
          <span key={c.code} className={`term-city ${on ? "on" : ""}`} title={`${c.what} ${on ? "trading" : "closed"} (holidays not shown)`}>
            <i />{c.code} <b className="num">{p.text}</b>
          </span>
        );
      })}
      <span className="term-city you" title={`Your time (${you})`}>YOU <b className="num">{parts(now, you).text}</b></span>
      <span className="term-city on hl" title="Hyperliquid never closes"><i />HL <b>24/7</b></span>
    </div>
  );
}

// ── what changed ──────────────────────────────────────────────────────────
/* The change log comes from the server (api/edgelog.py), so it reads the same after a reload or on another device. */

/* Compares each reply with the last one and keeps a running log: price moves past a threshold since the last line
   for that market, the real markets opening and shutting, roll steps, events about to land, sources going down. */
const KIND = { up: "MOVE", down: "MOVE", flow: "FLOW", sess: "HOURS", roll: "ROLL", cal: "SOON", alert: "ALERT", sys: "SYS" };

export function ChangeLog({ log }) {
  const today = new Date().toDateString();
  const time = (ms) => {
    const x = new Date(ms);
    const hms = `${pad2(x.getHours())}:${pad2(x.getMinutes())}:${pad2(x.getSeconds())}`;
    return x.toDateString() === today ? hms : `${x.toLocaleDateString([], { weekday: "short" })} ${hms.slice(0, 5)}`;
  };
  return (
    <div className="term-log" role="log" aria-live="polite">
      {!log.length && <div className="muted">Waiting for the first change…</div>}
      {log.map((x) => (
        <div key={x.id} className={`term-log-line ${x.kind} ${x.side || ""}`}>
          <span className="muted num">{time(x.t)}</span>
          <span className={`term-log-kind ${x.kind} ${x.side || ""}`}>{KIND[x.kind] || x.kind.toUpperCase()}</span>
          <span>{x.text}</span>
        </div>
      ))}
    </div>
  );
}

// ── status bar ────────────────────────────────────────────────────────────
const SRC = { "Hyperliquid xyz": "HL xyz", "Hourly candles": "candles", "Futures prices": "futures" };
const age = (ms) => (ms < 60_000 ? `${Math.round(ms / 1000)}s` : ms < 3_600_000 ? `${Math.round(ms / 60000)}m` : `${(ms / 3_600_000).toFixed(1)}h`);

export function StatusBar({ d, error, now, latency, lastFetch, every }) {
  const utc = new Date(now);
  const next = lastFetch ? Math.max(0, Math.ceil((lastFetch + every - now) / 1000)) : null;
  return (
    <footer className="term-status">
      <span className={error ? "bad" : "good"}><i />{error ? "OFFLINE" : d?.mode === "replay" ? "REPLAY" : "LIVE"}</span>
      <span>API <b className="num">{latency == null ? "-" : `${Math.round(latency)} ms`}</b></span>
      <span>refresh <b className="num">{next == null ? "-" : `${next}s`}</b></span>
      {Object.entries(d?.status || {}).map(([src, s]) => (
        <span key={src} className={s.ok ? "good" : "bad"} title={s.error || `last read ${s.last_ms ? new Date(s.last_ms).toLocaleTimeString() : "-"}`}>
          <i />{SRC[src] || src} <b className="num">{s.last_ms ? age(now - s.last_ms) : "-"}</b>
        </span>
      ))}
      <span className="term-status-right">
        <span><kbd>/</kbd> command</span>
        <span><kbd>1</kbd><kbd>2</kbd><kbd>3</kbd> tabs</span>
        <span>UTC <b className="num">{pad2(utc.getUTCHours())}:{pad2(utc.getUTCMinutes())}:{pad2(utc.getUTCSeconds())}</b></span>
      </span>
    </footer>
  );
}

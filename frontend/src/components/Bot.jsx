import { useEffect, useRef, useState } from "react";
import { setSound, soundOn } from "../botSound.js";

/* The bot: big-mover alerts. It watches the wallets whose trades are followed by big moves and calls the trade the
   moment one of them buys or sells. One screen: what it's doing, four numbers, the latest alerts. Settings, the full
   record, the wallet list and how it works sit behind the tabs. */

const usd = (x, sign = false) =>
  x == null ? "-" : `${x < 0 ? "−" : sign && x > 0 ? "+" : ""}$${Math.abs(x).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
const usdk = (x) => (x == null ? "-" : x >= 1000 ? `$${(x / 1000).toFixed(1)}k` : `$${Math.round(x)}`);
const bps = (x) => (x == null ? "-" : `${x > 0 ? "+" : x < 0 ? "−" : ""}${Math.abs(x).toFixed(1)} bps`);
const pct = (x, sign = true) => (x == null ? "-" : `${sign && x > 0 ? "+" : x < 0 ? "−" : ""}${Math.abs(x * 100).toFixed(x !== 0 && Math.abs(x) < 0.1 ? 1 : 0)}%`);
const tone = (x) => (x == null || Math.abs(x) < 1e-9 ? "" : x > 0 ? "up" : "down");
const clock = (ms) => new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" });
const ago = (ms, now) => {
  const s = Math.max(0, (now - ms) / 1000);
  return s < 60 ? `${Math.round(s)}s ago` : s < 3600 ? `${Math.round(s / 60)} min ago` : `${(s / 3600).toFixed(1)} h ago`;
};
const short = (w) => (w && w.length > 12 ? `${w.slice(0, 6)}…${w.slice(-4)}` : w);
const sideWord = (s) => (s > 0 ? "LONG" : "SHORT");
const STATUS = { live: "Live", paused: "Paused", warming: "Warming up", off: "Off" };
const TABS = [["settings", "Settings"], ["record", "Record"], ["wallets", "Wallets"], ["how", "How it works"]];

function headline(d) {
  const { status, settings: c, list, live } = d;
  if (status === "off") return "Off. Turn it on in Settings to get alerts.";
  if (status === "warming") {
    if (list.state === "failed") return `Couldn't build the big-mover list: ${list.error || "unknown error"}.`;
    if (list.state === "building" || list.state === "loading")
      return `Building the big-mover list from the last ${c.history_days} days of recordings…`;
    return `Only ${list.count} big movers qualify so far. Alerts start at ${c.min_list}.`;
  }
  if (status === "paused") return `Paused: ${d.pause_reason}. Still watching, and alerts come back when it recovers.`;
  const a = live?.alert;
  if (a) {
    const closeAt = a.t_ms + (a.fill_s + a.hold_s) * 1000;
    return `Get in within ${a.fill_s}s, close at ${clock(closeAt)}.`;
  }
  const last = d.alerts.find((x) => x.announced);
  const watching = `Watching ${list.count} big movers on ${c.coins.join(" and ")}.`;
  if (!last) return `${watching} No alerts yet.`;
  return `${watching} Last alert: ${last.coin} ${sideWord(last.side).toLowerCase()} ${ago(last.t_ms, d.now_ms)}${last.settled ? ` (${bps(last.bps)})` : ""}.`;
}

export default function Bot({ onClose }) {
  const [d, setD] = useState(null);
  const [error, setError] = useState(null);
  const [tab, setTab] = useState(null);
  const alive = useRef(true);

  const load = async () => {
    try {
      const r = await fetch("/api/bot");
      const j = await r.json();
      if (!alive.current) return;
      if (r.ok) {
        setD(j);
        setError(null);
      } else setError(j.detail || "Couldn't read the bot.");
    } catch {
      if (alive.current) setError("Couldn't reach the server.");
    }
  };

  useEffect(() => {
    alive.current = true;
    load();
    const t = setInterval(load, 2000);
    return () => {
      alive.current = false;
      clearInterval(t);
    };
  }, []);

  const call = async (method, url, body) => {
    try {
      const r = await fetch(url, { method, headers: { "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : undefined });
      const j = await r.json();
      if (!r.ok) throw new Error(j.detail || "That didn't work.");
      setD(j);
      setError(null);
      return true;
    } catch (e) {
      setError(e.message);
      return false;
    }
  };

  const status = d?.status;
  return (
    <div className="planner-scrim" onClick={onClose}>
      <aside className="planner bm" role="dialog" aria-label="Bot" onClick={(e) => e.stopPropagation()}>
        <div className="panel-head">
          <h2 className="panel-title">Bot</h2>
          <span className="bm-head-right">
            {status && <span className={`bm-status ${status}`}>{STATUS[status]}</span>}
            <button className="plan-close" onClick={onClose} aria-label="Close">✕</button>
          </span>
        </div>
        {error && <div className="plan-error">{error}</div>}
        {!d ? (
          <p className="plan-note muted" style={{ padding: 12 }}>{error ? "" : "Loading…"}</p>
        ) : (
          <>
            <Hero d={d} />
            <Numbers d={d} />
            <Latest d={d} />
            <div className="bm-tabs" role="tablist">
              {TABS.map(([k, label]) => (
                <button key={k} role="tab" aria-selected={tab === k} className={`plan-chip ${tab === k ? "on" : ""}`}
                  onClick={() => setTab(tab === k ? null : k)}>{label}</button>
              ))}
            </div>
            {tab === "settings" && <Settings d={d} call={call} />}
            {tab === "record" && <Record d={d} />}
            {tab === "wallets" && <Wallets d={d} call={call} />}
            {tab === "how" && <How d={d} />}
          </>
        )}
      </aside>
    </div>
  );
}

function Hero({ d }) {
  const a = d.status === "live" ? d.live?.alert : null;
  if (!a) return <div className="bm-hero"><p className="bm-say">{headline(d)}</p></div>;
  const age = Math.max(0, (d.now_ms - a.t_ms) / 1000);
  const inLeft = Math.ceil(a.fill_s - age);
  const closeLeft = Math.ceil(a.fill_s + a.hold_s - age);
  return (
    <div className={`bm-hero bm-call ${a.side > 0 ? "long" : "short"}`}>
      <div className="bm-call-top">
        <b>{a.coin} {sideWord(a.side)}</b>
        <span>{inLeft > 0 ? `get in now · ${inLeft}s` : `close in ${Math.max(0, closeLeft)}s`}</span>
      </div>
      <p className="bm-say">{headline(d)}</p>
      <p className="bm-sub">
        {a.wallets > 1 ? `${a.wallets} big movers` : "A big mover"} {a.side > 0 ? "bought" : "sold"} {usdk(a.usd)}
        {a.mid ? ` at ${a.mid.toLocaleString(undefined, { maximumFractionDigits: 2 })}` : ""}. {d.settings.leverage}×,{" "}
        {usd(d.shadow.margin)} margin.
      </p>
    </div>
  );
}

function Numbers({ d }) {
  const sh = d.shadow;
  const change = sh.start ? sh.balance / sh.start - 1 : null;
  const lag = String(d.settings.fill_s);
  const wk = d.record.week[lag] || {};
  return (
    <div className="tiles bm-tiles">
      <div className="tile">
        <div className="tile-label">Shadow balance</div>
        <div className={`tile-value ${tone(change)}`}>{usd(sh.balance)}</div>
        <div className="tile-sub">{sh.busted ? "Out of margin: reset in Settings" : `${pct(change)} from ${usd(sh.start)} · ${sh.trades} trades`}</div>
      </div>
      <div className="tile">
        <div className="tile-label">Last {d.last50.n || 50} alerts</div>
        <div className={`tile-value ${tone(d.last50.bps)}`}>{bps(d.last50.bps)}</div>
        <div className="tile-sub">average, filled {lag}s late</div>
      </div>
      <div className="tile">
        <div className="tile-label">{d.settings.leverage}× per trade, 7 days</div>
        <div className={`tile-value ${tone(wk.pt)}`}>{wk.n ? pct(wk.pt) : "-"}</div>
        <div className="tile-sub">{wk.n ? `${pct(wk.win, false)} wins · ${pct(wk.busts, false)} busts` : "no alerts yet"}</div>
      </div>
      <div className="tile">
        <div className="tile-label">Alerts, last {d.today.hours >= 23.9 ? "24" : Math.max(1, Math.round(d.today.hours))} h</div>
        <div className="tile-value">{d.today.alerts}</div>
        <div className="tile-sub">about {d.today.per_hour.toFixed(1)} an hour</div>
      </div>
    </div>
  );
}

function Latest({ d }) {
  const [all, setAll] = useState(false);
  const rows = all ? d.alerts : d.alerts.slice(0, 5);
  return (
    <section className="plan-sec">
      <h3>Latest alerts</h3>
      {!rows.length ? (
        <p className="plan-note muted" style={{ margin: 0 }}>None yet.</p>
      ) : (
        <div className="bm-list">
          {rows.map((a) => (
            <div key={a.id} className={`bm-row ${a.announced ? "" : "quiet"}`}>
              <span className="muted">{clock(a.t_ms)}</span>
              <span><b>{a.coin}</b> <span className={a.side > 0 ? "up" : "down"}>{sideWord(a.side).toLowerCase()}</span></span>
              {!a.announced ? (
                <span className="muted bm-span">not called: {a.why === "warming" ? "warming up" : a.why}</span>
              ) : !a.settled ? (
                <span className="muted bm-span">in progress</span>
              ) : a.bps == null ? (
                <span className="muted bm-span">feed gap, not scored</span>
              ) : (
                <>
                  <span className={tone(a.bps)}>{bps(a.bps)}</span>
                  <span className={tone(a.pt)}>{a.pt <= -1 ? "bust" : pct(a.pt)}</span>
                </>
              )}
            </div>
          ))}
        </div>
      )}
      {d.alerts.length > 5 && (
        <button className="plan-chip bm-more" onClick={() => setAll(!all)}>{all ? "Fewer" : `All ${d.alerts.length}`}</button>
      )}
    </section>
  );
}

const MONEY = [["balance", "Balance", "$"], ["margin_pct", "Margin per trade", "%"], ["fill_s", "Your fill delay", "s"], ["hold_s", "Hold", "s"]];
const LIST = [["cutoff_bps", "Big mover from", "bps"], ["t_min", "Skill (t) at least", ""], ["min_n", "Decisions at least", ""],
  ["min_list", "Alerts need wallets", ""], ["history_days", "Learn from last", "days"]];
const LEVS = [200, 500, 841];

function Settings({ d, call }) {
  const c = d.settings;
  const [draft, setDraft] = useState(() => ({ ...c }));
  const [sound, setSnd] = useState(soundOn());
  const saved = JSON.stringify(c);
  useEffect(() => setDraft(JSON.parse(saved)), [saved]);
  const [busy, setBusy] = useState(false);
  const set = (k, v) => setDraft((x) => ({ ...x, [k]: v }));
  const changed = Object.keys(draft).filter((k) => JSON.stringify(draft[k]) !== JSON.stringify(c[k]));
  const save = async () => {
    setBusy(true);
    await call("PUT", "/api/bot/settings", Object.fromEntries(changed.map((k) => [k, draft[k]])));
    setBusy(false);
  };
  const flag = (k, label) => (
    <button className={`plan-chip ${draft[k] ? "on" : ""}`} onClick={() => set(k, !draft[k])}>{label} {draft[k] ? "on" : "off"}</button>
  );
  const field = ([k, label, unit]) => (
    <label key={k} className="plan-field">
      {label}
      <span className="plan-input">
        {unit === "$" && "$"}
        <input value={draft[k]} onChange={(e) => set(k, e.target.value)} inputMode="decimal" aria-label={label} />
        {unit !== "$" && unit}
      </span>
    </label>
  );
  const money = ["balance", "leverage", "margin_pct"].some((k) => changed.includes(k));
  return (
    <section className="plan-sec bm-settings">
      <div className="plan-holds">
        {flag("on", "Bot")}
        {flag("telegram", "Telegram")}
        {flag("auto_pause", "Auto-pause")}
        <button className={`plan-chip ${sound ? "on" : ""}`} onClick={() => { setSound(!sound); setSnd(!sound); }}>
          Sound {sound ? "on" : "off"}
        </button>
      </div>
      <div className="plan-holds" role="group" aria-label="Coins">
        {d.known_coins.map((k) => {
          const on = draft.coins.includes(k);
          return (
            <button key={k} className={`plan-chip ${on ? "on" : ""}`}
              onClick={() => set("coins", on ? draft.coins.filter((x) => x !== k) : [...draft.coins, k])}>{k}</button>
          );
        })}
      </div>
      <label className="plan-field">
        Leverage
        <span className="bm-lev">
          <span className="plan-input small"><input value={draft.leverage} onChange={(e) => set("leverage", e.target.value)} inputMode="decimal" aria-label="Leverage" />×</span>
          {LEVS.map((x) => (
            <button key={x} className={`plan-chip ${Number(draft.leverage) === x ? "on" : ""}`} onClick={() => set("leverage", x)}>{x}×</button>
          ))}
        </span>
      </label>
      {MONEY.map(field)}
      <details className="bm-adv">
        <summary>Big-mover list</summary>
        {LIST.map(field)}
      </details>
      <div className="plan-holds">
        <button className="plan-open" disabled={!changed.length || busy} onClick={save}>{busy ? "Saving…" : "Save"}</button>
        <button className="plan-chip" onClick={() => call("POST", "/api/bot/reset")}>Reset shadow balance</button>
      </div>
      {money && <p className="plan-note muted">Changing balance, leverage or margin starts the shadow balance again.</p>}
    </section>
  );
}

function Record({ d }) {
  const [per, setPer] = useState("week");
  const rec = d.record[per] || {};
  const lags = Object.keys(rec).sort((a, b) => Number(a) - Number(b));
  return (
    <section className="plan-sec">
      <div className="plan-holds">
        {[["day", "24 h"], ["week", "7 days"], ["all", "All"]].map(([k, label]) => (
          <button key={k} className={`plan-chip ${per === k ? "on" : ""}`} onClick={() => setPer(k)}>{label}</button>
        ))}
      </div>
      <table className="plan-table">
        <thead>
          <tr><th>Filled</th><th>Alerts</th><th>Move</th><th>Wins</th><th>{d.settings.leverage}×</th><th>Busts</th></tr>
        </thead>
        <tbody>
          {lags.map((k) => {
            const r = rec[k];
            return (
              <tr key={k} className={k === String(d.settings.fill_s) ? "bm-you" : ""}>
                <td>{k}s late{k === String(d.settings.fill_s) ? " (you)" : ""}</td>
                <td>{r.n}</td>
                <td className={tone(r.bps)}>{r.n ? bps(r.bps) : "-"}</td>
                <td>{r.n ? pct(r.win, false) : "-"}</td>
                <td className={tone(r.pt)}>{r.n ? pct(r.pt) : "-"}</td>
                <td>{r.n ? pct(r.busts, false) : "-"}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <p className="plan-note muted">Every alert, settled from the live book: in at the mid that many seconds after it, out after the hold. Papertrade's cut on wins included.</p>
      <Twaps d={d} per={per} />
    </section>
  );
}

const TWAP_HOLDS = [["300", "5 min"], ["900", "15 min"], ["1800", "30 min"], ["end", "To the end"]];

function Twaps({ d, per }) {
  const t = d.twaps;
  if (!t) return null;
  const rec = t.periods[per] || {};
  return (
    <>
      <h3 className="bm-sub-head">Silent test: big TWAPs</h3>
      <p className="plan-note muted" style={{ marginTop: 0 }}>
        Not called, only scored. A wallet sending $20k+ every 30s, in 6s after its 4th slice, its way. {t.spotted_24h} spotted in 24 h.
      </p>
      <table className="plan-table">
        <thead>
          <tr><th>Hold</th><th>Seen</th><th>Move</th><th>Wins</th><th>{d.settings.leverage}×</th><th>Busts</th></tr>
        </thead>
        <tbody>
          {TWAP_HOLDS.map(([k, label]) => {
            const r = rec[k] || { n: 0 };
            return (
              <tr key={k}>
                <td>{label}</td>
                <td>{r.n}</td>
                <td className={tone(r.bps)}>{r.n ? bps(r.bps) : "-"}</td>
                <td>{r.n ? pct(r.win, false) : "-"}</td>
                <td className={tone(r.pt)}>{r.n ? pct(r.pt) : "-"}</td>
                <td>{r.n ? pct(r.busts, false) : "-"}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </>
  );
}

function Wallets({ d, call }) {
  const l = d.list;
  const [all, setAll] = useState(false);
  return (
    <section className="plan-sec">
      <p className="plan-note" style={{ marginTop: 0 }}>
        {l.count} big movers
        {l.built_ms ? ` · built ${ago(l.built_ms, Date.now())} from ${l.hours} h of trades (${(l.decisions || 0).toLocaleString()} decisions)` : ""}
        {l.state === "building" ? " · rebuilding now…" : ""}
      </p>
      {l.wallets?.length > 0 && (
        <div className="bot-scroll">
          <table className="plan-table">
            <thead><tr><th>Wallet</th><th>Avg move</th><th>Skill (t)</th><th>Decisions</th><th>Coins</th><th>Last seen</th></tr></thead>
            <tbody>
              {(all ? l.wallets : l.wallets.slice(0, 15)).map((w) => (
                <tr key={w.wallet}>
                  <td title={w.wallet}>{short(w.wallet)}</td>
                  <td className="up">{bps(w.mean_bps)}</td>
                  <td>{w.t.toFixed(1)}</td>
                  <td>{w.n}</td>
                  <td>{(w.coins || []).join(", ")}</td>
                  <td className="muted">{w.seen_ms ? ago(w.seen_ms, d.now_ms) : "-"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <div className="plan-holds bm-more">
        {l.wallets?.length > 15 && <button className="plan-chip" onClick={() => setAll(!all)}>{all ? "Fewer" : `All ${l.wallets.length}`}</button>}
        <button className="plan-chip" disabled={l.state === "building"} onClick={() => call("POST", "/api/bot/refresh")}>Rebuild now</button>
      </div>
    </section>
  );
}

function How({ d }) {
  const c = d.settings;
  return (
    <section className="plan-sec">
      <ol className="bm-how">
        <li>A few wallets' trades are usually followed by a big move their way. The bot finds them from the last {c.history_days} days of trades and refreshes the list every hour.</li>
        <li>When one of them buys or sells ${Math.round(c.min_usd).toLocaleString()}+ on {c.coins.join(" or ")}, you get the call: coin, side, a sound and Telegram.</li>
        <li>Go the same way as fast as you can and close after {c.hold_s}s. Every second counts: in tests the edge was about +6.5 bps at 2s and +3.9 at 6s.</li>
        <li>Every alert is scored from the live book. If the last 50 average below zero, alerts pause until they recover.</li>
      </ol>
    </section>
  );
}

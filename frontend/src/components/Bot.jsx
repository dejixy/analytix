import { useEffect, useRef, useState } from "react";
import { setSound, soundOn } from "../botSound.js";

/* The bot: big TWAPs. A wallet selling or buying $20k+ every 30 seconds tends to push price its way. The bot spots
   them live, shows them while they run, scores every one and (with alerts on) calls them. One screen: what it's
   doing, four numbers, the TWAPs running now and the latest ones. Settings, the record and how it works sit behind
   the tabs. */

const usd = (x, sign = false) =>
  x == null ? "-" : `${x < 0 ? "−" : sign && x > 0 ? "+" : ""}$${Math.abs(x).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
const usdk = (x) => (x == null ? "-" : x >= 1e6 ? `$${(x / 1e6).toFixed(2)}M` : x >= 1000 ? `$${(x / 1000).toFixed(1)}k` : `$${Math.round(x)}`);
const bps = (x) => (x == null ? "-" : `${x > 0 ? "+" : x < 0 ? "−" : ""}${Math.abs(x).toFixed(1)} bps`);
const pct = (x, sign = true) => (x == null ? "-" : `${sign && x > 0 ? "+" : x < 0 ? "−" : ""}${Math.abs(x * 100).toFixed(x !== 0 && Math.abs(x) < 0.1 ? 1 : 0)}%`);
const tone = (x) => (x == null || Math.abs(x) < 1e-9 ? "" : x > 0 ? "up" : "down");
const clock = (ms) => new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
const mins = (s) => (s < 60 ? `${Math.max(0, Math.round(s))}s` : `${Math.floor(s / 60)}m ${String(Math.round(s % 60)).padStart(2, "0")}s`);
const ago = (ms, now) => {
  const s = Math.max(0, (now - ms) / 1000);
  return s < 60 ? `${Math.round(s)}s ago` : s < 3600 ? `${Math.round(s / 60)} min ago` : `${(s / 3600).toFixed(1)} h ago`;
};
const sideWord = (s) => (s > 0 ? "LONG" : "SHORT");
const HOLD_LABEL = { 300: "5 min", 900: "15 min", 1800: "30 min", end: "Until it stops" };
const holdWords = (k) => (k === "end" ? "until the TWAP stops" : `for ${HOLD_LABEL[k]}`);
const STATUS = { live: "Alerts on", silent: "Silent" };
const TABS = [["settings", "Settings"], ["record", "Record"], ["how", "How it works"]];

function headline(d) {
  const c = d.settings;
  const a = d.status === "live" ? d.live?.alert : null;
  if (a) {
    const age = (d.now_ms - a.t_ms) / 1000;
    if (age < a.fill_s) return `Get in within ${Math.ceil(a.fill_s - age)}s, ${a.side > 0 ? "long" : "short"}.`;
    if (a.hold_s != null) return `Close at ${clock(a.t_ms + (a.fill_s + a.hold_s) * 1000)}.`;
    return a.running ? "Hold while the TWAP keeps going. You'll be told when it stops." : "The TWAP stopped. Close now.";
  }
  const where = c.coins.join(" and ");
  const run = d.running.length;
  const off = d.status === "silent" ? " Alerts are off." : "";
  if (run) return `${run} big TWAP${run > 1 ? "s" : ""} running on ${where} now.${off}`;
  const last = d.recent[0];
  return `Watching for big TWAPs on ${where}.${last ? ` Last one ${ago(last.end_ms || last.t_ms, d.now_ms)}.` : " None yet."}${off}`;
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
          <h2 className="panel-title">Bot · big TWAPs</h2>
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
            <Running d={d} />
            <Latest d={d} />
            <div className="bm-tabs" role="tablist">
              {TABS.map(([k, label]) => (
                <button key={k} role="tab" aria-selected={tab === k} className={`plan-chip ${tab === k ? "on" : ""}`}
                  onClick={() => setTab(tab === k ? null : k)}>{label}</button>
              ))}
            </div>
            {tab === "settings" && <Settings d={d} call={call} />}
            {tab === "record" && <Record d={d} />}
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
  const right = inLeft > 0 ? `get in now · ${inLeft}s`
    : a.hold_s != null ? `close in ${mins(a.fill_s + a.hold_s - age)}`
    : a.running ? `running ${mins(age)}` : "close now";
  return (
    <div className={`bm-hero bm-call ${a.side > 0 ? "long" : "short"}`}>
      <div className="bm-call-top">
        <b>{a.coin} {sideWord(a.side)}</b>
        <span>{right}</span>
      </div>
      <p className="bm-say">{headline(d)}</p>
      <p className="bm-sub">
        A wallet is {a.side > 0 ? "buying" : "selling"} about {usdk(a.slice_usd)} every 30s ({usdk(a.usd)} so far)
        {a.mid ? `, spotted at ${a.mid.toLocaleString(undefined, { maximumFractionDigits: 2 })}` : ""}.
        {a.plan ? ` Its plan: ${usdk(a.plan.total_usd)} over ${a.plan.minutes} min, ${usdk(a.plan.left_usd)} still to come, ending ${clock(a.plan.ends_ms)}.` : ""}{" "}
        {d.settings.leverage}×, {usd(d.shadow.margin)} margin.
      </p>
    </div>
  );
}

function Numbers({ d }) {
  const sh = d.shadow;
  const change = sh.start ? sh.balance / sh.start - 1 : null;
  const k = d.settings.hold;
  const wk = (d.record.week[d.settings.alert_on] || {})[k] || {};
  return (
    <div className="tiles bm-tiles">
      <div className="tile">
        <div className="tile-label">Shadow balance</div>
        <div className={`tile-value ${tone(change)}`}>{usd(sh.balance)}</div>
        <div className="tile-sub">{sh.busted ? "Out of margin: reset in Settings" : `${pct(change)} from ${usd(sh.start)} · ${sh.trades} trades`}</div>
      </div>
      <div className="tile">
        <div className="tile-label">Average, 7 days{d.settings.alert_on === "strong" ? ", strong" : ""}</div>
        <div className={`tile-value ${tone(wk.bps)}`}>{wk.n ? bps(wk.bps) : "-"}</div>
        <div className="tile-sub">{wk.n ? `${wk.n} TWAPs · ${pct(wk.win, false)} went its way` : "none finished yet"}</div>
      </div>
      <div className="tile">
        <div className="tile-label">{d.settings.leverage}× per trade, 7 days</div>
        <div className={`tile-value ${tone(wk.pt)}`}>{wk.n ? pct(wk.pt) : "-"}</div>
        <div className="tile-sub">held {HOLD_LABEL[k].toLowerCase()}{wk.n ? ` · ${pct(wk.busts, false)} busts` : ""}</div>
      </div>
      <div className="tile">
        <div className="tile-label">Spotted, last {d.today.hours >= 23.9 ? "24" : Math.max(1, Math.round(d.today.hours))} h</div>
        <div className="tile-value">{d.today.spotted}</div>
        <div className="tile-sub">{d.today.strong} strong · about {d.today.per_day.toFixed(0)} a day · {d.running.length} running</div>
      </div>
    </div>
  );
}

function Running({ d }) {
  if (!d.running.length) return null;
  return (
    <section className="plan-sec">
      <h3>Running now</h3>
      <div className="bm-list">
        {d.running.map((r) => (
          <div key={r.id} className="bm-row bm-row-run">
            <span className="muted">{mins((d.now_ms - r.t_ms) / 1000)}</span>
            <span><b>{r.coin}</b> <span className={r.side > 0 ? "up" : "down"}>{sideWord(r.side).toLowerCase()}</span></span>
            <span className="muted">{usdk(r.slice_usd)} × {r.slices}</span>
            <span className={tone(r.move_now)} title="How far price has moved its way since 6s after it was spotted">{bps(r.move_now)}</span>
            <span className="muted bm-plan">
              <Strength r={r} />
              {r.plan ? ` · plan ${usdk(r.plan.total_usd)} over ${r.plan.minutes} min · ${usdk(r.plan.left_usd)} to come · ends ${clock(r.plan.ends_ms)}` : ""}
            </span>
          </div>
        ))}
      </div>
    </section>
  );
}

function Latest({ d }) {
  const [all, setAll] = useState(false);
  const rows = all ? d.recent : d.recent.slice(0, 5);
  return (
    <section className="plan-sec">
      <h3>Latest, held {HOLD_LABEL[d.settings.hold].toLowerCase()}</h3>
      {!rows.length ? (
        <p className="plan-note muted" style={{ margin: 0 }}>None finished yet. Big TWAPs come about 18 times a day, fewer at weekends.</p>
      ) : (
        <div className="bm-list">
          {rows.map((r) => (
            <div key={r.id} className={`bm-row ${r.announced ? "" : "quiet"}`}>
              <span className="muted">{clock(r.t_ms)}</span>
              <span><b>{r.coin}</b> <span className={r.side > 0 ? "up" : "down"}>{sideWord(r.side).toLowerCase()}</span> <span className="muted">{usdk(r.usd)}</span> <Strength r={r} /></span>
              {!r.settled ? (
                <span className="muted bm-span">{r.move_now == null ? "feed gap, not scored" : "settling"}</span>
              ) : (
                <>
                  <span className={tone(r.bps)}>{bps(r.bps)}</span>
                  <span className={tone(r.pt)}>{r.pt <= -1 ? "bust" : pct(r.pt)}</span>
                </>
              )}
            </div>
          ))}
        </div>
      )}
      {d.recent.length > 5 && (
        <button className="plan-chip bm-more" onClick={() => setAll(!all)}>{all ? "Fewer" : `All ${d.recent.length}`}</button>
      )}
    </section>
  );
}

function Strength({ r }) {
  if (r.strong) return <span className="up">strong</span>;
  if (r.known) return <span className="muted">small</span>;
  return <span className="muted">no plan</span>;
}

const MONEY = [["balance", "Balance", "$"], ["margin_pct", "Margin per trade", "%"]];
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
  const money = ["balance", "leverage", "margin_pct", "hold"].some((k) => changed.includes(k));
  return (
    <section className="plan-sec bm-settings">
      <div className="plan-holds">
        {flag("alerts", "Alerts")}
        {flag("telegram", "Telegram")}
        <button className={`plan-chip ${sound ? "on" : ""}`} onClick={() => { setSound(!sound); setSnd(!sound); }}>
          Sound {sound ? "on" : "off"}
        </button>
      </div>
      <label className="plan-field">
        Alert on
        <span className="bm-lev">
          {[["strong", "Strong ones"], ["all", "All"]].map(([k, label]) => (
            <button key={k} className={`plan-chip ${draft.alert_on === k ? "on" : ""}`} onClick={() => set("alert_on", k)}>{label}</button>
          ))}
        </span>
      </label>
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
        Hold
        <span className="bm-lev">
          {d.holds.map((k) => (
            <button key={k} className={`plan-chip ${draft.hold === k ? "on" : ""}`} onClick={() => set("hold", k)}>{HOLD_LABEL[k]}</button>
          ))}
        </span>
      </label>
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
      <div className="plan-holds">
        <button className="plan-open" disabled={!changed.length || busy} onClick={save}>{busy ? "Saving…" : "Save"}</button>
        <button className="plan-chip" onClick={() => call("POST", "/api/bot/reset")}>Reset shadow balance</button>
      </div>
      {money && <p className="plan-note muted">Changing balance, leverage, margin or hold starts the shadow balance again.</p>}
    </section>
  );
}

function Record({ d }) {
  const [per, setPer] = useState("week");
  const [group, setGroup] = useState(d.settings.alert_on);
  const rec = (d.record[per] || {})[group] || {};
  return (
    <section className="plan-sec">
      <div className="plan-holds">
        {[["day", "24 h"], ["week", "7 days"], ["all", "All"]].map(([k, label]) => (
          <button key={k} className={`plan-chip ${per === k ? "on" : ""}`} onClick={() => setPer(k)}>{label}</button>
        ))}
        <span className="bm-gap" />
        {[["strong", "Strong"], ["all", "Every TWAP"]].map(([k, label]) => (
          <button key={k} className={`plan-chip ${group === k ? "on" : ""}`} onClick={() => setGroup(k)}>{label}</button>
        ))}
      </div>
      <table className="plan-table">
        <thead>
          <tr><th>Held</th><th>TWAPs</th><th>Move</th><th>Wins</th><th>{d.settings.leverage}×</th><th>Busts</th></tr>
        </thead>
        <tbody>
          {d.holds.map((k) => {
            const r = rec[k] || { n: 0 };
            const you = k === d.settings.hold;
            return (
              <tr key={k} className={you ? "bm-you" : ""}>
                <td>{HOLD_LABEL[k]}{you ? " (you)" : ""}</td>
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
      <p className="plan-note muted">
        Every big TWAP, settled from the live book: in at the mid {d.fill_s}s after it was spotted, out after the hold.
        Papertrade's cut on wins included. Strong = $250k+ still to come and 2%+ of the coin's usual trading while it
        runs. In the test week, strong ones made about +9 bps at 15 min and the rest about nothing. Judge it on 100 or
        more strong ones. Plans read from Hyperliquid: {d.plans.found} of {d.plans.asked} asked.
      </p>
    </section>
  );
}

function How({ d }) {
  return (
    <section className="plan-sec">
      <ol className="bm-how">
        <li>Hyperliquid's TWAP orders buy or sell in slices every 30 seconds. When one wallet does that with $20k+ slices, price has tended to drift its way while the rest comes in.</li>
        <li>The bot asks Hyperliquid for the wallet's TWAP order as soon as two slices land (about 30 seconds in), so it knows the full size and when it ends. Without an answer, it still spots it at the 4th slice.</li>
        <li>Strong ones still have $250k+ to come and are 2%+ of the coin's usual trading while they run. In the test, only those moved price their way. With alerts on you get the call for {d.settings.alert_on === "strong" ? "strong ones" : "every one"} on {d.settings.coins.join(" or ")}: coin, side, a sound, and Telegram if it's on.</li>
        <li>Go the same way within {d.fill_s}s and hold {holdWords(d.settings.hold)}. Being a little late matters less here than for most signals.</li>
        <li>Every TWAP is scored from the live book, alerts on or off. It's still a test: one week of history found it, a week of live results decides it.</li>
      </ol>
    </section>
  );
}

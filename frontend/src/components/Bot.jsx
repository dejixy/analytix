import { useEffect, useRef, useState } from "react";
import { setSound, soundOn } from "../botSound.js";

/* The bot: baskets of strategies, each with its own balance. The panel shows the baskets the server lists (Degen
   only, by default); the others keep running and recording in the background. With one basket the strip of basket
   tabs is left out. Each basket: what it's doing, its numbers, its trades, and settings, record and how it works
   behind the tabs. */

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
const STATUS = { live: "Alerts on", silent: "Silent", research: "In research", testing: "Testing", warming: "Warming up" };
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
  const [basket, setBasket] = useState(null);            // null: the server's first basket
  const alive = useRef(true);
  const current = useRef(null);

  const load = async () => {
    const want = current.current;
    try {
      const r = await fetch(want ? `/api/bot?basket=${want}` : "/api/bot");
      const j = await r.json();
      if (!alive.current || want !== current.current) return;
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
    current.current = basket;
    setD(null);
    setTab(null);
    load();
    const t = setInterval(load, 2000);
    return () => {
      alive.current = false;
      clearInterval(t);
    };
  }, [basket]);

  const call = async (method, path, body) => {
    try {
      const r = await fetch(`${path}?basket=${basket ?? d?.basket ?? ""}`, { method, headers: { "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : undefined });
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
  const shown = d?.baskets || [];
  const on = basket ?? d?.basket;
  return (
    <div className="planner-scrim" onClick={onClose}>
      <aside className="planner bm" role="dialog" aria-label="Bot" onClick={(e) => e.stopPropagation()}>
        <div className="panel-head">
          <h2 className="panel-title">{shown.length === 1 ? `Bot · ${shown[0].name}` : "Bot"}</h2>
          <span className="bm-head-right">
            {status && <span className={`bm-status ${status}`}>{STATUS[status]}</span>}
            <button className="plan-close" onClick={onClose} aria-label="Close">✕</button>
          </span>
        </div>
        {shown.length > 1 && <div className="bm-baskets" role="tablist" aria-label="Baskets">
          {shown.map((b) => (
            <button key={b.key} role="tab" aria-selected={on === b.key} className={`bm-basket ${on === b.key ? "on" : ""}`}
              onClick={() => setBasket(b.key)}>
              <b>{b.name}</b>
              <span className={b.status === "warming" ? "muted" : tone(b.balance - b.start)}>
                {b.status === "warming" ? "warming up" : b.balance != null ? `${usd(b.balance)}${b.status === "testing" ? " · test" : ""}` : ""}
              </span>
            </button>
          ))}
        </div>}
        {error && <div className="plan-error">{error}</div>}
        {!d ? (
          <p className="plan-note muted" style={{ padding: 12 }}>{error ? "" : "Loading…"}</p>
        ) : d.basket === "informed" ? (
          <Informed d={d} call={call} />
        ) : d.basket === "degen" ? (
          <DegenBasket d={d} call={call} />
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
  const money = ["leverage", "hold"].some((k) => changed.includes(k));
  const resize = ["balance", "margin_pct"].some((k) => changed.includes(k));
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
      {money && <p className="plan-note muted">Changing leverage or hold starts the shadow balance again.</p>}
      {!money && resize && <p className="plan-note muted">The shadow keeps its result: a new balance carries the profit or loss over, and trades use the new size from the next one.</p>}
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
        {[["strong", "Strong"], ["noplan", "No plan"], ["all", "Every TWAP"]].map(([k, label]) => (
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
        more strong ones. No plan = Hyperliquid had no TWAP order for the wallet (often a trader's own slicing bot on the
        same beat), so how much was left couldn't be known; not traded, scored to see if it should be. Plans read from
        Hyperliquid: {d.plans.found} of {d.plans.asked} asked.
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


/* Basket 2: fading the reliably wrong. A live test: every hidden order is scored, the fades are shadow-traded on
   Hyperliquid (all coins, limit orders) and Papertrade (BTC/ETH). */

const shortW = (w) => (w ? `${w.slice(0, 6)}…${w.slice(-4)}` : "");

function Informed({ d, call }) {
  const [tab, setTab] = useState(null);
  return (
    <>
      <InfHero d={d} />
      <InfNumbers d={d} />
      <InfOpen d={d} />
      <InfLatest d={d} />
      <div className="bm-tabs" role="tablist">
        {TABS.map(([k, label]) => (
          <button key={k} role="tab" aria-selected={tab === k} className={`plan-chip ${tab === k ? "on" : ""}`}
            onClick={() => setTab(tab === k ? null : k)}>{label}</button>
        ))}
      </div>
      {tab === "settings" && <InfSettings d={d} call={call} />}
      {tab === "record" && <InfRecord d={d} />}
      {tab === "how" && <InfHow d={d} />}
    </>
  );
}

function infHeadline(d) {
  const sc = d.scores;
  if (d.status === "warming") {
    if (sc.state === "failed") return `Couldn't build the wallet scores: ${sc.error || "unknown error"}.`;
    return "Building wallet scores from the recordings (a few minutes)…";
  }
  const off = d.settings.alerts ? "" : " Alerts are off: it's a test, scored in the background.";
  const w = sc.faded_wallets === 1 ? "1 wallet whose pushes reliably reverse" : `${sc.faded_wallets} wallets whose pushes reliably reverse`;
  return `Fading ${w}, on ${d.settings.coins.join(", ")}. ${d.today.fades} fade${d.today.fades === 1 ? "" : "s"} out of ${d.today.orders} hidden orders in 24 h.${off}`;
}

function InfHero({ d }) {
  const a = d.live?.alert;
  if (!a) return <div className="bm-hero"><p className="bm-say">{infHeadline(d)}</p></div>;
  const age = Math.max(0, (d.now_ms - a.t_ms) / 1000);
  const inLeft = Math.ceil(a.fill_s - age);
  return (
    <div className={`bm-hero bm-call ${a.side > 0 ? "long" : "short"}`}>
      <div className="bm-call-top">
        <b>{a.coin} {sideWord(a.side)}</b>
        <span>{inLeft > 0 ? `get in now · ${inLeft}s` : `close in ${mins(a.fill_s + a.hold_s - age)}`}</span>
      </div>
      <p className="bm-say">Fade: a wallet that's usually wrong is {a.side > 0 ? "selling" : "buying"} in pieces ({usdk(a.usd)} so far, score {a.score?.toFixed(1)}).</p>
    </div>
  );
}

function InfNumbers({ d }) {
  const hl = d.shadows.hl;
  const pt = d.shadows.pt;
  const wk = d.record.week.fade || {};
  const all = d.record.week.all || {};
  const ch = (sh) => (sh.start ? sh.balance / sh.start - 1 : null);
  return (
    <div className="tiles bm-tiles">
      <div className="tile">
        <div className="tile-label">Hyperliquid shadow</div>
        <div className={`tile-value ${tone(ch(hl))}`}>{usd(hl.balance)}</div>
        <div className="tile-sub">{hl.busted ? "Out of margin: reset in Settings" : `${pct(ch(hl))} · ${hl.trades} trades · ${d.settings.coins.join(", ")}, limit orders`}</div>
      </div>
      <div className="tile">
        <div className="tile-label">Papertrade shadow</div>
        <div className={`tile-value ${tone(ch(pt))}`}>{usd(pt.balance)}</div>
        <div className="tile-sub">{pt.busted ? "Out of margin: reset in Settings"
          : d.settings.coins.some((c) => c === "BTC" || c === "ETH") ? `${pct(ch(pt))} · ${pt.trades} trades · BTC, ETH, ${d.settings.leverage}×`
          : "not used: SOL and HYPE aren't on Papertrade"}</div>
      </div>
      <div className="tile">
        <div className="tile-label">Fades, 7 days</div>
        <div className={`tile-value ${tone(wk.bps)}`}>{wk.n ? bps(wk.bps) : "-"}</div>
        <div className="tile-sub">{wk.n ? `${wk.n} · ${pct(wk.win, false)} wins · every order ${bps(all.bps)}` : "none finished yet"}</div>
      </div>
      <div className="tile">
        <div className="tile-label">Last 24 h</div>
        <div className="tile-value">{d.today.fades}</div>
        <div className="tile-sub">fades · {d.today.orders} hidden orders · {d.open.length} open</div>
      </div>
    </div>
  );
}

function InfOpen({ d }) {
  if (!d.open.length) return null;
  return (
    <section className="plan-sec">
      <h3>Open now</h3>
      <div className="bm-list">
        {d.open.map((r) => (
          <div key={r.id} className="bm-row bm-row-run">
            <span className="muted">{mins((d.now_ms - r.t_ms) / 1000)}</span>
            <span><b>{r.coin}</b> <span className={r.side > 0 ? "up" : "down"}>{sideWord(r.side).toLowerCase()}</span></span>
            <span className="muted" title={r.wallet}>{shortW(r.wallet)} {r.score?.toFixed(1)}</span>
            <span className={tone(r.move_now)}>{bps(r.move_now)}</span>
          </div>
        ))}
      </div>
    </section>
  );
}

function InfLatest({ d }) {
  const [all, setAll] = useState(false);
  const rows = all ? d.recent : d.recent.slice(0, 5);
  return (
    <section className="plan-sec">
      <h3>Latest fades, held 15 min</h3>
      {!rows.length ? (
        <p className="plan-note muted" style={{ margin: 0 }}>None finished yet. In the test, about 66 a day across the four coins.</p>
      ) : (
        <div className="bm-list">
          {rows.map((r) => (
            <div key={r.id} className={`bm-row ${r.announced || !d.settings.alerts ? "" : "quiet"}`}>
              <span className="muted">{clock(r.t_ms)}</span>
              <span><b>{r.coin}</b> <span className={r.side > 0 ? "up" : "down"}>{sideWord(r.side).toLowerCase()}</span> <span className="muted">{usdk(r.usd)}</span></span>
              {r.bps == null ? (
                <span className="muted bm-span">feed gap, not scored</span>
              ) : (
                <>
                  <span className={tone(r.bps)} title={r.dip != null ? `Went ${bps(-r.dip)} against you at worst` : undefined}>{bps(r.bps)}</span>
                  <span className={tone(r.hl)} title="On Hyperliquid, limit orders, capped leverage">{r.hl <= -1 ? "bust" : pct(r.hl)}</span>
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

function InfSettings({ d, call }) {
  const c = d.settings;
  const [draft, setDraft] = useState(() => ({ ...c }));
  const saved = JSON.stringify(c);
  useEffect(() => setDraft(JSON.parse(saved)), [saved]);
  const set = (k, v) => setDraft((x) => ({ ...x, [k]: v }));
  const changed = Object.keys(draft).filter((k) => JSON.stringify(draft[k]) !== JSON.stringify(c[k]));
  const flag = (k, label) => (
    <button className={`plan-chip ${draft[k] ? "on" : ""}`} onClick={() => set(k, !draft[k])}>{label} {draft[k] ? "on" : "off"}</button>
  );
  const field = (k, label, unit) => (
    <label key={k} className="plan-field">
      {label}
      <span className="plan-input">
        {unit === "$" && "$"}
        <input value={draft[k]} onChange={(e) => set(k, e.target.value)} inputMode="decimal" aria-label={label} />
        {unit !== "$" && unit}
      </span>
    </label>
  );
  const money = ["leverage", "threshold"].some((k) => changed.includes(k));
  const resize = ["balance", "margin_pct"].some((k) => changed.includes(k));
  return (
    <section className="plan-sec bm-settings">
      <div className="plan-holds">
        {flag("alerts", "Alerts")}
        {flag("telegram", "Telegram")}
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
      {field("threshold", "Fade wallets scoring at most", "bps")}
      {field("leverage", "Leverage", "×")}
      {field("balance", "Balance", "$")}
      {field("margin_pct", "Margin per trade", "%")}
      <div className="plan-holds">
        <button className="plan-open" disabled={!changed.length} onClick={() => call("PUT", "/api/bot/settings", Object.fromEntries(changed.map((k) => [k, draft[k]])))}>Save</button>
        <button className="plan-chip" onClick={() => call("POST", "/api/bot/reset")}>Reset shadow balances</button>
      </div>
      <p className="plan-note muted">
        Hyperliquid caps leverage per coin: {Object.entries(d.hl_lev).map(([k, v]) => `${k} ${v}×`).join(", ")} at your setting.
        {money ? " Changing leverage or the score starts both shadows again." : resize ? " Both shadows keep their result: a new balance carries the profit or loss over, and trades use the new size from the next one." : ""}
      </p>
    </section>
  );
}

function InfRecord({ d }) {
  const [per, setPer] = useState("week");
  const rec = d.record[per] || {};
  const rows = [["fade", `Fades (score ${d.settings.threshold} or worse)`], ["all", "Every hidden order (control)"]];
  return (
    <section className="plan-sec">
      <div className="plan-holds">
        {[["day", "24 h"], ["week", "7 days"], ["all", "All"]].map(([k, label]) => (
          <button key={k} className={`plan-chip ${per === k ? "on" : ""}`} onClick={() => setPer(k)}>{label}</button>
        ))}
      </div>
      <div className="bot-scroll">
        <table className="plan-table">
          <thead>
            <tr><th>Faded</th><th>n</th><th>Move</th><th>Wins</th><th>HL taker</th><th>HL limit</th><th>HL ×</th><th>Papertrade</th></tr>
          </thead>
          <tbody>
            {rows.map(([k, label]) => {
              const r = rec[k] || { n: 0 };
              return (
                <tr key={k} className={k === "fade" ? "bm-you" : ""}>
                  <td>{label}</td>
                  <td>{r.n}</td>
                  <td className={tone(r.bps)}>{r.n ? bps(r.bps) : "-"}</td>
                  <td>{r.n ? pct(r.win, false) : "-"}</td>
                  <td className={tone(r.taker)}>{r.n ? bps(r.taker) : "-"}</td>
                  <td className={tone(r.maker)}>{r.n ? bps(r.maker) : "-"}</td>
                  <td className={tone(r.hl)}>{r.n ? pct(r.hl) : "-"}</td>
                  <td className={tone(r.pt)}>{r.pt != null ? `${pct(r.pt)} (${r.pt_n})` : "-"}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {rec.fade?.dip != null && (
        <p className="plan-note">
          Dip before closing (how far a fade went against you first): usually <b className="down">{bps(-rec.fade.dip)}</b>,
          at worst <b className="down">{bps(-rec.fade.dip_max)}</b>.
        </p>
      )}
      <p className="plan-note muted">
        Every hidden order is settled as if faded: in at the mid {d.fill_s}s after it's spotted, out {d.hold_s / 60} min later.
        HL taker / limit = the move after Hyperliquid fees ({d.costs.taker} / {d.costs.maker} bps a round trip). HL × = per trade on margin at
        each coin's capped leverage, limit-order fees. Papertrade = BTC and ETH only, at {d.settings.leverage}×, its cut on wins. In the test:
        fades +7.5 bps, every order +1.1.
      </p>
    </section>
  );
}

function InfHow({ d }) {
  const sc = d.scores;
  return (
    <section className="plan-sec">
      <ol className="bm-how">
        <li>The bot spots hidden orders: a wallet buying or selling one way in pieces, $50k+ over a minute or more.</li>
        <li>Every few hours it scores every wallet from the recordings: on average, what did price do 15 minutes after its hidden orders? Some wallets' pushes reliably reverse. Last built {sc.built_ms ? ago(sc.built_ms, Date.now()) : "-"}: {sc.wallets} wallets, {sc.faded_wallets} at {d.settings.threshold} or worse.</li>
        <li>When one of those starts a new hidden order, the fade is the other side, within {d.fill_s}s, held {d.hold_s / 60} min. Being a little late barely mattered in the test.</li>
        <li>It fades SOL and HYPE: the fade check found the edge there, while BTC and ETH fades lost money at Hyperliquid's leverage. BTC and ETH are still watched, so the record's control covers all four.</li>
        <li>Fades come in bunches, and the later ones did better, so each coin can hold its own open fade. Only Hyperliquid lists SOL and HYPE, and there the edge only beats fees with limit orders, so the shadow uses limit-order fees.</li>
      </ol>
    </section>
  );
}

/* ── Basket 3, degen: the informed basket's SOL and HYPE fades, a big share of the pot each, weeks of 2x or -50% ── */

function DegenBasket({ d, call }) {
  const [tab, setTab] = useState(null);
  return (
    <>
      <div className="bm-hero"><p className="bm-say">{dgHeadline(d)}</p></div>
      <DgNumbers d={d} />
      <DgOpen d={d} />
      <DgLatest d={d} />
      <div className="bm-tabs" role="tablist">
        {TABS.map(([k, label]) => (
          <button key={k} role="tab" aria-selected={tab === k} className={`plan-chip ${tab === k ? "on" : ""}`}
            onClick={() => setTab(tab === k ? null : k)}>{label}</button>
        ))}
      </div>
      {tab === "settings" && <DgSettings d={d} call={call} />}
      {tab === "record" && <DgRecord d={d} />}
      {tab === "how" && <DgHow d={d} />}
    </>
  );
}

function dgHeadline(d) {
  if (d.status === "warming") return "Waiting for the informed basket's wallet scores…";
  return `Trading the informed basket's fades on ${d.settings.coins.join(", ")} with ${d.settings.pot_pct}% of the pot on each, at Hyperliquid's max leverage. A week ends at ${d.up}× or −${Math.round((1 - d.down) * 100)}%. Paper only: it's a test.`;
}

function DgNumbers({ d }) {
  const ch = d.start ? d.pot / d.start - 1 : null;
  const left = d.week_left_s == null ? "starts at the next fade" : `${Math.ceil(d.week_left_s / 86400)}d left`;
  const c = d.count;
  return (
    <div className="tiles bm-tiles">
      <div className="tile">
        <div className="tile-label">This week's pot</div>
        <div className={`tile-value ${tone(ch)}`}>{usd(d.pot)}</div>
        <div className="tile-sub">{pct(ch)} · {d.week_trades} trades · {left}</div>
      </div>
      <div className="tile">
        <div className="tile-label">The week ends at</div>
        <div className="tile-value">{usd(d.start * d.up)}</div>
        <div className="tile-sub">or down at {usd(d.start * d.down)}, or after 7 days</div>
      </div>
      <div className="tile">
        <div className="tile-label">Weeks</div>
        <div className="tile-value">{c["2x"]} · {c["-50%"]} · {c.neither}</div>
        <div className="tile-sub">doubled · halved · neither</div>
      </div>
      <div className="tile">
        <div className="tile-label">Banked</div>
        <div className={`tile-value ${tone(d.banked)}`}>{usd(d.banked, true)}</div>
        <div className="tile-sub">{d.trades} trades · {d.trades ? pct(d.wins / d.trades, false) : "-"} won{d.busts ? ` · ${d.busts} liquidated` : ""}</div>
      </div>
    </div>
  );
}

function DgOpen({ d }) {
  if (!d.open.length) return null;
  return (
    <section className="plan-sec">
      <h3>Open now</h3>
      <div className="bm-list">
        {d.open.map((o) => (
          <div key={o.id} className="bm-row bm-row-run">
            <span className="muted">{mins((d.now_ms - o.t_ms) / 1000)}</span>
            <span><b>{o.coin}</b> <span className={o.side > 0 ? "up" : "down"}>{sideWord(o.side).toLowerCase()}</span> <span className="muted">{usd(o.margin)} · {o.lev}×</span></span>
            <span className={tone(o.pnl_now)}>{o.pnl_now == null ? "-" : usd(o.pnl_now, true)}</span>
            <span className={tone(o.move_now)}>{bps(o.move_now)}</span>
          </div>
        ))}
      </div>
    </section>
  );
}

function DgLatest({ d }) {
  const [all, setAll] = useState(false);
  const rows = all ? d.recent : d.recent.slice(0, 5);
  return (
    <section className="plan-sec">
      <h3>Latest trades, held {d.hold_s / 60} min</h3>
      {!rows.length ? (
        <p className="plan-note muted" style={{ margin: 0 }}>None yet. In the test, about 26 a day on SOL and HYPE.</p>
      ) : (
        <div className="bm-list">
          {rows.map((t) => (
            <div key={t.id} className="bm-row">
              <span className="muted">{clock(t.t_ms)}</span>
              <span><b>{t.coin}</b> <span className={t.side > 0 ? "up" : "down"}>{sideWord(t.side).toLowerCase()}</span> <span className="muted">{usd(t.margin)}</span></span>
              <span className={tone(t.pnl)}>{t.bust ? "liquidated" : usd(t.pnl, true)}</span>
              <span className="muted" title="The pot after it">{usd(t.pot)}</span>
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

function DgRecord({ d }) {
  return (
    <section className="plan-sec">
      {!d.weeks.length ? (
        <p className="plan-note muted" style={{ margin: 0 }}>No week has ended yet.</p>
      ) : (
        <div className="bm-list">
          {d.weeks.map((w) => (
            <div key={w.since} className="bm-row">
              <span className="muted">{new Date(w.since).toLocaleDateString([], { month: "short", day: "numeric" })}</span>
              <span>{w.result === "2x" ? "doubled" : w.result === "-50%" ? "halved" : "neither"} <span className="muted">· {w.trades} trades</span></span>
              <span className={tone(w.pot - d.start)}>{usd(w.pot - d.start, true)}</span>
              <span className="muted">{usd(w.pot)}</span>
            </div>
          ))}
        </div>
      )}
      <p className="plan-note muted">
        Each week starts again from {usd(d.start)}. Banked = what every finished week ended at, minus {usd(d.start)}, added up.
        A trade still open when a week ends lands in the next one.
      </p>
    </section>
  );
}

function DgSettings({ d, call }) {
  const c = d.settings;
  const [draft, setDraft] = useState(() => ({ ...c }));
  const saved = JSON.stringify(c);
  useEffect(() => setDraft(JSON.parse(saved)), [saved]);
  const set = (k, v) => setDraft((x) => ({ ...x, [k]: v }));
  const changed = Object.keys(draft).filter((k) => JSON.stringify(draft[k]) !== JSON.stringify(c[k]));
  const field = (k, label, unit) => (
    <label key={k} className="plan-field">
      {label}
      <span className="plan-input">
        {unit === "$" && "$"}
        <input value={draft[k]} onChange={(e) => set(k, e.target.value)} inputMode="decimal" aria-label={label} />
        {unit !== "$" && unit}
      </span>
    </label>
  );
  return (
    <section className="plan-sec bm-settings">
      <div className="plan-holds" role="group" aria-label="Coins">
        {d.known_coins.map((k) => {
          const on = draft.coins.includes(k);
          return (
            <button key={k} className={`plan-chip ${on ? "on" : ""}`}
              onClick={() => set("coins", on ? draft.coins.filter((x) => x !== k) : [...draft.coins, k])}>{k}</button>
          );
        })}
      </div>
      {field("pot_pct", "Share of the pot per trade", "%")}
      {field("balance", "Balance each week starts from", "$")}
      <div className="plan-holds">
        <button className="plan-open" disabled={!changed.length} onClick={() => call("PUT", "/api/bot/settings", Object.fromEntries(changed.map((k) => [k, draft[k]])))}>Save</button>
        <button className="plan-chip" onClick={() => call("POST", "/api/bot/reset")}>Start again</button>
      </div>
      <p className="plan-note muted">
        Leverage is each coin's max on Hyperliquid: {Object.entries(d.hl_lev).map(([k, v]) => `${k} ${v}×`).join(", ")}. It fades wallets scoring {d.threshold} or worse (the informed basket's setting; that basket keeps running in the background).
        {changed.includes("pot_pct") ? " A new share applies from the next trade." : ""}
        {changed.includes("balance") ? " A new balance applies from the next week (or now, if no week is running)." : ""}
      </p>
    </section>
  );
}

function DgHow({ d }) {
  return (
    <section className="plan-sec">
      <ol className="bm-how">
        <li>It takes the informed basket's fades: a wallet whose pushes reliably reverse starts a hidden order, and the trade is the other side, {d.fill_s}s later, held {d.hold_s / 60} min.</li>
        <li>Only {d.settings.coins.join(" and ")}: the fade check found the edge there (about +1% of the margin a trade, 26 a day in the test). One open trade per coin.</li>
        <li>Each trade puts {d.settings.pot_pct}% of the pot, as it stands when the trade opens, up as margin at Hyperliquid's max leverage, with limit-order fees. If price reaches the liquidation point, that margin is lost.</li>
        <li>A week ends at {usd(d.start * d.up)} (doubled) or {usd(d.start * d.down)} (halved), or after 7 days. The next week starts again from {usd(d.start)}, and what each week ended at is banked.</li>
        <li>Paper only, no alerts: it tests whether the fades can double a pot. With no edge, betting this big loses more often than it wins.</li>
      </ol>
    </section>
  );
}

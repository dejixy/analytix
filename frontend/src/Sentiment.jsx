import { useEffect, useState } from "react";
import { fmtTime } from "./format.js";

/* Sentiment Check (/sentiment, /api/sentiment): when the market looked like it does now, what did price do next, from
   10 seconds to 24 hours, over five years of Binance candles (three years of 1-second candles for the shortest).
   Green and red only mark a lean that held up on later years; everything else is white. */

const COINS = ["BTC", "ETH", "SOL", "HYPE"];
const EVERY_MS = 15_000;
const H_NAME = { "10s": "10 sec", "30s": "30 sec", "1m": "1 min", "2m": "2 min", "5m": "5 min", "10m": "10 min", "15m": "15 min",
  "30m": "30 min", "1h": "1 hour", "4h": "4 hours", "24h": "24 hours" };
const F_NAME = { vol: "Volatility", news: "News", move: "Move so far", volume: "Volume", session: "Session", buyers: "Taker flow",
  oi: "Open interest", funding: "Funding" };
const pct = (p, d = 0) => (p == null || Number.isNaN(p) ? "-" : `${(p * 100).toFixed(d)}%`);
const move = (bps, d = 2) => (bps == null || Number.isNaN(bps) ? "-" : `${bps >= 0 ? "+" : "−"}${Math.abs(bps / 100).toFixed(d)}%`);
const size = (bps) => (bps == null ? "-" : `${(bps / 100).toFixed(bps < 10 ? 3 : 2)}%`);
const skill = (x) => (x == null ? "-" : `${x >= 0 ? "+" : "−"}${Math.abs(x * 100).toFixed(1)}%`);
const skillTone = (x) => (x == null ? "" : x > 0.005 ? "up" : x < -0.005 ? "down" : "");
const lean = (h) => (h.held_up?.held ? (h.p_up > h.base_p_up ? "up" : "down") : "");
const ago = (ms) => {
  if (!ms) return "-";
  const d = Math.round((Date.now() - ms) / 86_400_000);
  return d < 1 ? "today" : d === 1 ? "yesterday" : `${d} days ago`;
};
const day = (s) => (s ? new Date(s * 1000).toISOString().slice(0, 10) : "-");

const Seg = ({ value, set, opts }) => (
  <span className="az-seg" role="group">
    {opts.map((k) => <button key={k} className={value === k ? "on" : ""} aria-pressed={value === k} onClick={() => set(k)}>{k}</button>)}
  </span>
);
const Top = ({ title, facts = [], children }) => (
  <div className="az-top">
    <h2>{title}</h2>
    {children}
    <span className="az-facts">{facts.filter(Boolean).map((f, i) => <span key={i}>{f}</span>)}</span>
  </div>
);

export default function Sentiment() {
  const [coin, setCoin] = useState(() => new URLSearchParams(window.location.search).get("coin")?.toUpperCase() || "BTC");
  const [d, setD] = useState(null);
  const [error, setError] = useState(null);
  const [pick, setPick] = useState("10m");
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    document.title = `Analytix · Sentiment check · ${coin}`;
    let live = true;
    let timer;
    setD((old) => (old?.coin === coin ? old : null));
    const load = async () => {
      try {
        const r = await fetch(`/api/sentiment?coin=${coin}`);
        const j = await r.json();
        if (!live) return;
        if (r.ok) {
          setD(j);
          setError(null);
        } else setError(j.detail || "Couldn't read the market");
      } catch {
        if (live) setError("Can't reach the Analytix server");
      }
      if (live) timer = setTimeout(load, EVERY_MS);
    };
    load();
    return () => {
      live = false;
      clearTimeout(timer);
    };
  }, [coin]);
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);
  const hs = d?.horizons || [];
  const sel = hs.find((h) => h.horizon === pick) || hs.find((h) => h.horizon === "10m") || hs[0];
  const coins = [...new Set([...COINS, ...(d?.coins || [])])];

  return (
    <div className="app term az">
      <header className="topbar">
        <div className="brand">
          <span className="brand-name">ANALYTIX<span className="cursor" aria-hidden="true" /></span>
          <span className="brand-tag">sentiment check · when it looked like this</span>
        </div>
        <div className="header-right">
          <a className="alerts-open term-link" href="/analyzer">Analyzer ›</a>
          <a className="alerts-open term-link" href="/">‹ Dashboard</a>
          <span className="badge" title={error || ""}>
            <span className={`dot ${error ? "critical" : "good pulse"}`} />
            {error ? "Offline" : d?.source ? `Live · ${d.source}` : "Live"}
          </span>
          <span className="clock num" title="Your local time">{fmtTime(now)}</span>
        </div>
      </header>
      <div className="sc-bar">
        <Seg value={coin} set={(c) => { setCoin(c); window.history.replaceState(null, "", `/sentiment?coin=${c}`); }} opts={coins} />
        {d?.now?.long && <span className="num sc-price">{d.now.long.price.toLocaleString(undefined, { maximumFractionDigits: d.now.long.price < 10 ? 4 : 2 })}</span>}
        {d?.history?.long && <span className="muted">5-minute to 24-hour reads from {day(d.history.long.from)}; 10-second to 2-minute from {day(d.history.short?.from)}</span>}
      </div>
      {!d && <div className="term-boot" role="status"><div>&gt; {error || "reading the market"}<span className="cursor" aria-hidden="true" /></div></div>}
      {d?.missing && <p className="az-note">No tables for {coin} yet. Build them on the server: bash ~/analytix/scripts/run.sh sentiment</p>}
      {d?.offline && <p className="az-note">Sentiment Check reads the live market, so it runs in live mode only.</p>}
      {hs.length > 0 && (
        <div className="term-grid">
          <div className="term-wide"><Horizons hs={hs} sel={sel} setPick={setPick} coin={coin} /></div>
          <div className="tw-7"><Detail h={sel} coin={coin} /></div>
          <div className="tw-5 term-col">
            <Now d={d} h={sel} />
            <Drivers h={sel} />
          </div>
          <div className="term-wide"><Checks hs={hs} built={d.built} /></div>
        </div>
      )}
    </div>
  );
}

function Horizons({ hs, sel, setPick, coin }) {
  return (
    <section className="panel">
      <Top title={`${coin} like now: what came next`} />
      <div className="az-scroll">
        <table className="az-table az-click">
          <thead>
            <tr>
              <th>Next</th><th className="r">Times</th><th className="r">Finished higher</th><th className="r">Usually</th>
              <th className="r" title="The lean learned from earlier years, tested on each later year">Held up</th>
              <th className="r">Typical move</th><th className="r">Middle 80%</th>
              <th className="r">Dips a typical move</th><th className="r">Rises a typical move</th>
            </tr>
          </thead>
          <tbody>
            {hs.map((h) => {
              const t = lean(h);
              const dip = h.dips?.[1];
              const rise = h.rises?.[1];
              return (
                <tr key={h.horizon} className={sel?.horizon === h.horizon ? "on" : ""} onClick={() => setPick(h.horizon)} tabIndex={0}
                  onKeyDown={(e) => e.key === "Enter" && setPick(h.horizon)}>
                  <td><b>{H_NAME[h.horizon]}</b></td>
                  <td className="r num">{h.n.toLocaleString()}</td>
                  <td className={`r num ${t}`}><b className={t}>{h.up.toLocaleString()}</b> of {h.n.toLocaleString()} · {pct(h.p_up)}</td>
                  <td className="r num muted">{pct(h.base_p_up)}</td>
                  <td className={`r num ${t}`}>{h.held_up?.tested ? (h.held_up.held ? `yes, ${h.held_up.agree} of ${h.held_up.tested} years` : `no, ${h.held_up.agree ?? 0} of ${h.held_up.tested}`) : "-"}</td>
                  <td className="r num">±{size(h.typical_bps)}</td>
                  <td className="r num">{move(h.q10_bps)} to {move(h.q90_bps)}</td>
                  <td className="r num">{pct(dip?.chance)} <span className="muted">vs {pct(dip?.base)}</span></td>
                  <td className="r num">{pct(rise?.chance)} <span className="muted">vs {pct(rise?.base)}</span></td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function Curve({ c, typical }) {
  const W = 640, H = 220, PL = 34, PR = 10, PT = 10, PB = 26;
  const lim = 3.2 * typical;
  const pts = c.bps.map((x, i) => [x, c.now[i], c.all[i]]).filter(([x]) => Math.abs(x) <= lim + 1e-9);
  if (pts.length < 3) return null;
  const x0 = pts[0][0], x1 = pts[pts.length - 1][0];
  const X = (x) => PL + ((x - x0) / (x1 - x0)) * (W - PL - PR);
  const Y = (y) => PT + (1 - y) * (H - PT - PB);
  const path = (k) => pts.map((p, i) => `${i ? "L" : "M"}${X(p[0]).toFixed(1)},${Y(p[k]).toFixed(1)}`).join("");
  const ticks = [-2, -1, 0, 1, 2].map((m) => m * typical).filter((x) => x >= x0 && x <= x1);
  return (
    <svg className="sc-curve" viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Share of cases that finished below each move, like now and over all five years">
      {[0, 0.5, 1].map((y) => (
        <g key={y}>
          <line x1={PL} x2={W - PR} y1={Y(y)} y2={Y(y)} className="sc-grid" />
          <text x={PL - 6} y={Y(y) + 3} className="sc-ax" textAnchor="end">{y * 100}%</text>
        </g>
      ))}
      {ticks.map((x) => (
        <g key={x}>
          <line x1={X(x)} x2={X(x)} y1={PT} y2={H - PB} className={x === 0 ? "sc-zero" : "sc-grid"} />
          <text x={X(x)} y={H - 8} className="sc-ax" textAnchor="middle">{move(x, x === 0 ? 0 : 2)}</text>
        </g>
      ))}
      <path d={path(2)} className="sc-all" />
      <path d={path(1)} className="sc-now" />
    </svg>
  );
}

function Detail({ h, coin }) {
  if (!h) return null;
  const t = lean(h);
  return (
    <section className="panel">
      <Top title={`Next ${H_NAME[h.horizon]}`} facts={[`${h.n.toLocaleString()} times like now`, `${h.base_n.toLocaleString()} in all`]} />
      <div className="az-pad">
        <p className="sc-say">
          When {coin} looked like this, it finished the next {H_NAME[h.horizon]} higher <b className={t}>{h.up} times out of {h.n}</b>{" "}
          ({pct(h.p_up)} against {pct(h.base_p_up)} usually); the middle move was {move(h.q50_bps)}, and 8 in 10 ended between{" "}
          {move(h.q10_bps)} and {move(h.q90_bps)}.{" "}
          {h.held_up?.held ? <b className={t}>That lean held up on later years.</b> : "That lean hasn't held up on later years, so treat direction as a coin flip."}
        </p>
        <div className="sc-legend"><span className="sc-key now" />like now <span className="sc-key all" />all years <span className="muted">share of times it finished below each move</span></div>
        {h.curve && <Curve c={h.curve} typical={h.typical_bps} />}
      </div>
      <div className="az-scroll">
        <table className="az-table">
          <thead><tr><th>Within {H_NAME[h.horizon]}</th>{h.dips.map((x) => <th key={x.bps} className="r">{size(x.bps)}</th>)}</tr></thead>
          <tbody>
            <tr><td>Dipped at least</td>{h.dips.map((x) => <td key={x.bps} className="r num">{pct(x.chance)} <span className="muted">vs {pct(x.base)}</span></td>)}</tr>
            <tr><td>Rose at least</td>{h.rises.map((x) => <td key={x.bps} className="r num">{pct(x.chance)} <span className="muted">vs {pct(x.base)}</span></td>)}</tr>
          </tbody>
        </table>
      </div>
      <div className="az-scroll">
        <table className="az-table">
          <thead><tr><th>Year</th>{h.years.map((y) => <th key={y} className="r">{y}</th>)}</tr></thead>
          <tbody>
            <tr><td>Like now</td>{h.by_year.map(([n], i) => <td key={i} className="r num">{n || "-"}</td>)}</tr>
            <tr><td>Higher</td>{h.by_year.map(([n, u], i) => <td key={i} className="r num">{n ? pct(u / n) : "-"}</td>)}</tr>
          </tbody>
        </table>
      </div>
    </section>
  );
}

function Now({ d, h }) {
  const rows = ["vol", "volume", "buyers", "oi", "funding", "session"];
  const L = d.now?.long || {};
  const S = d.now?.short || {};
  return (
    <section className="panel">
      <Top title="Conditions now" facts={[h && `matched for ${H_NAME[h.horizon]}: ${h.kept.length} of 8`]} />
      <table className="az-table">
        <thead><tr><th /><th>Last hour</th><th>Last minutes</th></tr></thead>
        <tbody>
          {rows.map((f) => (
            <tr key={f}>
              <td>{F_NAME[f]}</td>
              <td className={h?.family === "long" && h.kept.includes(f) ? "" : "muted"}>{L[f] ?? "-"}</td>
              <td className={h?.family === "short" && h.kept.includes(f) ? "" : "muted"}>{S[f] ?? "-"}</td>
            </tr>
          ))}
          {h && (
            <>
              <tr><td>{F_NAME.move}</td><td colSpan={2} className={h.kept.includes("move") ? "" : "muted"}>{h.move}, over the last {H_NAME[h.horizon]}</td></tr>
              <tr><td>{F_NAME.news}</td><td colSpan={2} className={h.kept.includes("news") ? "" : "muted"}>{h.news}</td></tr>
            </>
          )}
        </tbody>
      </table>
      {h?.dropped?.length > 0 && <p className="az-note">Not matched (too few past cases with all of them): {h.dropped.map((f) => F_NAME[f].toLowerCase()).join(", ")}.</p>}
    </section>
  );
}

function Drivers({ h }) {
  if (!h?.drivers) return null;
  const side = (rows, cls) => rows.map((r) => (
    <tr key={r.feature + r.bin}>
      <td>{F_NAME[r.feature]}: {r.bin}</td>
      <td className={`r num ${cls}`}>{r.lift.toFixed(2)}×</td>
      <td className="r num muted">{r.n.toLocaleString()}</td>
    </tr>
  ));
  return (
    <section className="panel">
      <Top title={`Before big ${H_NAME[h.horizon]} moves`} facts={["beyond 1.5 typical moves"]} />
      <table className="az-table">
        <thead><tr><th>Came first</th><th className="r">vs usual</th><th className="r">Times</th></tr></thead>
        <tbody>
          <tr><td colSpan={3} className="up">Big rises</td></tr>
          {side(h.drivers.up, "up")}
          <tr><td colSpan={3} className="down">Big drops</td></tr>
          {side(h.drivers.down, "down")}
        </tbody>
      </table>
    </section>
  );
}

function Checks({ hs, built }) {
  return (
    <section className="panel">
      <Top title="Did reading like-now beat the plain rate?" facts={[built && `tables built ${ago(built * 1000)}`, "each year scored on the years before it"]} />
      <div className="az-scroll">
        <table className="az-table">
          <thead>
            <tr><th>Next</th><th className="r">Times scored</th><th className="r">Higher or lower</th><th className="r">How far it dips</th><th className="r">How far it rises</th></tr>
          </thead>
          <tbody>
            {hs.map((h) => (
              <tr key={h.horizon}>
                <td>{H_NAME[h.horizon]}</td>
                <td className="r num">{h.check?.n ? h.check.n.toLocaleString() : "-"}</td>
                <td className={`r num ${skillTone(h.check?.up)}`}>{skill(h.check?.up)}</td>
                <td className={`r num ${skillTone(h.check?.dip)}`}>{skill(h.check?.dip)}</td>
                <td className={`r num ${skillTone(h.check?.rise)}`}>{skill(h.check?.rise)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

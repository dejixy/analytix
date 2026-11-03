import { useEffect, useMemo, useRef, useState } from "react";

/* COMP: every edge market on one scale for the last 2 or 7 days (each line is % from where the range starts), with the
   hours the real market was shut shaded: those stretches are Hyperliquid pricing alone. A crosshair reads every line
   at any hour. The Links view shows which markets moved together, from hourly moves over the week. */

const COLOR = { CL: "#2fc493", BRENTOIL: "#7fd8c3", GOLD: "#e4ece8", SILVER: "#8fa39b", JPY: "#7c9cff", EUR: "#b197fc", DXY: "#f08cc0", GBP: "#9ad0ff" };
const SHORT = { CL: "WTI", BRENTOIL: "BRENT", JPY: "USD/JPY", EUR: "EUR/USD", DXY: "DXY", GBP: "GBP/USD" };
const tk = (name) => name.split(":").pop();
const label = (name) => SHORT[tk(name)] || tk(name);
const pct = (x, d = 2) => (x == null ? "-" : `${x > 0 ? "+" : x < 0 ? "−" : ""}${Math.abs(x).toFixed(d)}%`);
const when = (ms) => {
  const t = new Date(ms);
  return `${t.toLocaleDateString([], { weekday: "short" })} ${t.getDate()} ${t.toLocaleDateString([], { month: "short" })}, ${t.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hourCycle: "h23" })}`;
};

function niceStep(span) {
  const raw = span / 4;
  const p = 10 ** Math.floor(Math.log10(raw));
  return [1, 2, 2.5, 5, 10].map((k) => k * p).find((s) => s >= raw) || raw;
}

/* The chart is drawn at the box's real width (one unit, one pixel), so text and lines stay the same size on any screen. */
function Moves(props) {
  const box = useRef(null);
  const [w, setW] = useState(760);
  useEffect(() => {
    const el = box.current;
    if (!el || typeof ResizeObserver === "undefined") return undefined;
    const ro = new ResizeObserver(([e]) => setW(Math.max(300, Math.round(e.contentRect.width))));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return <div className="term-comp-chart" ref={box}><Chart {...props} W={w} /></div>;
}

function Chart({ c, days, on, W }) {
  const [hover, setHover] = useState(null);
  const H = W < 600 ? 220 : 270, L = 6, R = W < 600 ? 84 : 98, T = 14, B = 24;
  const n = c.series[Object.keys(c.series)[0]]?.length || 0;
  const from = Math.max(0, n - 1 - days * 24);
  const xs = Array.from({ length: n - from }, (_, k) => (from + k === n - 1 ? c.t_last : c.t0 + (from + k) * c.step));
  const lines = useMemo(() => Object.entries(c.series).filter(([name]) => on[name] !== false).map(([name, v]) => {
    const part = v.slice(from);
    const base = part.find((x) => x != null);
    return { name, pts: part.map((x) => (x == null || !base ? null : (x / base - 1) * 100)) };
  }), [c, from, on]);
  if (!xs.length) return <p className="plan-note muted term-pad">No history yet.</p>;
  if (!lines.length) return <p className="plan-note muted term-pad">Every line is hidden: click a name above to show it.</p>;
  const all = lines.flatMap((l) => l.pts).filter((x) => x != null);
  let lo = Math.min(0, ...all), hi = Math.max(0, ...all);
  const padY = (hi - lo || 1) * 0.08;
  lo -= padY; hi += padY;
  const x0 = xs[0], x1 = xs.at(-1);
  const x = (t) => L + ((t - x0) / (x1 - x0 || 1)) * (W - L - R);
  const y = (v) => T + (1 - (v - lo) / (hi - lo)) * (H - T - B);
  const step = niceStep(hi - lo);
  const ticks = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi; v += step) ticks.push(Number(v.toFixed(6)));
  // shut stretches: the gaps between the real market's open hours
  const shut = [];
  let cur = x0;
  for (const [a, b] of c.open) {
    if (b <= x0 || a >= x1) continue;
    if (a > cur) shut.push([cur, Math.min(a, x1)]);
    cur = Math.max(cur, b);
  }
  if (cur < x1) shut.push([cur, x1]);
  const mids = [];
  const dt = new Date(x0);
  dt.setHours(24, 0, 0, 0);
  for (; dt.getTime() < x1; dt.setDate(dt.getDate() + 1)) mids.push(dt.getTime());
  // end labels, nudged apart so they don't sit on each other
  const ends = lines.map((l) => {
    const last = [...l.pts].reverse().find((v) => v != null);
    return { name: l.name, v: last, y: last == null ? null : y(last) };
  }).filter((e) => e.y != null).sort((a, b) => a.y - b.y);
  for (let i = 1; i < ends.length; i++) if (ends[i].y - ends[i - 1].y < 11) ends[i].y = ends[i - 1].y + 11;

  const onMove = (e) => {
    const r = e.currentTarget.getBoundingClientRect();       // the inner box: exactly the chart's width
    const t = x0 + ((e.clientX - r.left) / r.width * W - L) / (W - L - R) * (x1 - x0);
    let best = 0;
    xs.forEach((v, i) => { if (Math.abs(v - t) < Math.abs(xs[best] - t)) best = i; });
    setHover(best);
  };
  const hx = hover != null ? x(xs[hover]) : null;
  const readout = hover != null ? lines.map((l) => ({ name: l.name, v: l.pts[hover] })).filter((r) => r.v != null).sort((a, b) => b.v - a.v) : [];
  return (
    <div onPointerMove={onMove} onPointerLeave={() => setHover(null)}>
      <svg viewBox={`0 0 ${W} ${H}`} width={W} height={H} role="img" aria-label={`Every market, % from ${days} days ago`}>
        <defs>
          <filter id="term-glow" x="-5%" y="-20%" width="110%" height="140%">
            <feGaussianBlur stdDeviation="1.6" result="b" />
            <feMerge><feMergeNode in="b" /><feMergeNode in="SourceGraphic" /></feMerge>
          </filter>
        </defs>
        {shut.map(([a, b]) => (
          <g key={a}>
            <rect x={x(a)} y={T} width={Math.max(0, x(b) - x(a))} height={H - T - B} className="term-comp-shut" />
            {x(b) - x(a) > 46 && <text x={(x(a) + x(b)) / 2} y={T + 11} textAnchor="middle" className="term-comp-shut-label">HL only</text>}
          </g>
        ))}
        {ticks.map((v) => (
          <g key={v}>
            <line x1={L} x2={W - R} y1={y(v)} y2={y(v)} className={v === 0 ? "term-comp-zero" : "term-comp-grid"} />
            <text x={L + 2} y={y(v) - 3} className="term-comp-ax">{pct(v, step < 0.1 ? 2 : 1)}</text>
          </g>
        ))}
        {mids.map((m) => (
          <g key={m}>
            <line x1={x(m)} x2={x(m)} y1={T} y2={H - B} className="term-comp-grid" />
            <text x={x(m) + 3} y={H - 8} className="term-comp-ax">{new Date(m).toLocaleDateString([], { weekday: "short", day: "numeric" })}</text>
          </g>
        ))}
        {lines.map((l) => {
          const d = [];
          l.pts.forEach((v, i) => { if (v != null) d.push(`${d.length ? "L" : "M"}${x(xs[i]).toFixed(1)},${y(v).toFixed(1)}`); });
          return <path key={l.name} d={d.join(" ")} stroke={COLOR[tk(l.name)] || "#a2b2ab"} className="term-comp-line" filter="url(#term-glow)" />;
        })}
        {ends.map((e) => (
          <g key={e.name}>
            <circle cx={W - R} cy={y(e.v)} r="2.4" fill={COLOR[tk(e.name)]} />
            <text x={W - R + 6} y={e.y + 3} className="term-comp-end" fill={COLOR[tk(e.name)]}>{label(e.name)} {pct(e.v, 1)}</text>
          </g>
        ))}
        {hx != null && (
          <g>
            <line x1={hx} x2={hx} y1={T} y2={H - B} className="term-comp-cross" />
            {lines.map((l) => l.pts[hover] != null && (
              <circle key={l.name} cx={hx} cy={y(l.pts[hover])} r="3" fill={COLOR[tk(l.name)]} className="term-comp-dot" />
            ))}
          </g>
        )}
      </svg>
      {hover != null && (
        <div className="term-comp-tip" style={{ left: `${(hx / W) * 100}%`, transform: `translateX(${hx > W * 0.6 ? "-104%" : "4%"})` }}>
          <div className="muted">{when(xs[hover])}</div>
          {readout.map((r) => (
            <div key={r.name}><i style={{ background: COLOR[tk(r.name)] }} />{label(r.name)} <b className="num">{pct(r.v)}</b></div>
          ))}
        </div>
      )}
    </div>
  );
}

function Links({ c }) {
  const names = c.corr.names;
  const m = c.corr.m;
  const pairs = [];
  names.forEach((a, i) => names.forEach((b, j) => { if (j > i && m[i][j] != null) pairs.push([a, b, m[i][j]]); }));
  const top = pairs.sort((p, q) => Math.abs(q[2]) - Math.abs(p[2])).slice(0, 3);
  const cell = (r) => (r == null ? undefined : r > 0 ? `rgba(31, 166, 122, ${0.08 + 0.6 * r})` : `rgba(229, 83, 95, ${0.08 + 0.55 * -r})`);
  return (
    <div className="term-pad">
      <div className="term-corr" style={{ gridTemplateColumns: `72px repeat(${names.length}, minmax(0, 1fr))` }}>
        <span />
        {names.map((n) => <span key={n} className="term-corr-h">{label(n)}</span>)}
        {names.map((a, i) => [
          <span key={`${a}-h`} className="term-corr-h left">{label(a)}</span>,
          ...names.map((b, j) => (
            <span key={`${a}-${b}`} className={`term-corr-c ${i === j ? "self" : ""}`} style={{ background: i === j ? undefined : cell(m[i][j]) }}
              title={i === j ? label(a) : `${label(a)} and ${label(b)}: ${m[i][j] == null ? "not enough shared hours" : m[i][j]}`}>
              {i === j ? "" : m[i][j] == null ? "-" : m[i][j].toFixed(2)}
            </span>
          )),
        ])}
      </div>
      {top.length > 0 && (
        <p className="plan-note">
          Strongest links this week:{" "}
          {top.map(([a, b, r], k) => (
            <span key={`${a}${b}`}>{k ? " · " : ""}<b>{label(a)}</b> {r > 0 ? "with" : "against"} <b>{label(b)}</b> <span className={r > 0 ? "up" : "down"}>{r.toFixed(2)}</span></span>
          ))}
        </p>
      )}
      <p className="plan-note muted">
        Each cell compares two markets' hourly moves over the last week: 1 moves exactly together, −1 exactly opposite,
        0 no link. USD/JPY rising means the yen falling.
      </p>
    </div>
  );
}

export default function Compare({ d }) {
  const c = d.compare;
  const [view, setView] = useState("moves");
  const [days, setDays] = useState(2);
  const [on, setOn] = useState({});
  if (!c || !Object.keys(c.series).length) return null;
  const names = Object.keys(c.series);
  return (
    <section id="p-COMP" className="panel term-wide">
      <div className="panel-head">
        <h2 className="panel-title"><span className="term-code">COMP</span>Moving together</h2>
        <span className="plan-holds">
          {[["moves", "Moves"], ["links", "Links"]].map(([k, t]) => (
            <button key={k} className={`plan-chip ${view === k ? "on" : ""}`} onClick={() => setView(k)}>{t}</button>
          ))}
          {view === "moves" && <span className="news-gap" />}
          {view === "moves" && [[2, "2 days"], [7, "7 days"]].map(([k, t]) => (
            <button key={k} className={`plan-chip ${days === k ? "on" : ""}`} onClick={() => setDays(k)}>{t}</button>
          ))}
        </span>
      </div>
      {view === "moves" ? (
        <>
          <div className="term-comp-legend">
            {names.map((n) => (
              <button key={n} className={`term-comp-key ${on[n] === false ? "off" : ""}`} aria-pressed={on[n] !== false}
                onClick={() => setOn((o) => ({ ...o, [n]: o[n] === false }))} title={on[n] === false ? "Show" : "Hide"}>
                <i style={{ background: COLOR[tk(n)] }} />{label(n)}
              </button>
            ))}
            <span className="muted term-comp-note">% from {days} days ago · shaded: {c.market} shut, Hyperliquid alone · hover to read</span>
          </div>
          <Moves c={c} days={days} on={on} />
        </>
      ) : (
        <Links c={c} />
      )}
    </section>
  );
}

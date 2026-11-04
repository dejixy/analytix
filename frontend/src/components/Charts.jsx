import { useEffect, useRef, useState } from "react";
import { Flash, Head, Pill } from "./TermPanel.jsx";

/* CHRT: one small chart per edge market, the way a terminal's chart wall reads: hourly closes ending at the live
   price, a price scale on the right with the last price tagged (green above yesterday, red below), yesterday's level
   dashed, and the hours the real market was shut shaded (Hyperliquid pricing alone). Hover reads any hour; click a
   chart to jump to its row. 2 or 7 days. */

const SHORT = { CL: "WTI", BRENTOIL: "BRENT", JPY: "USD/JPY", EUR: "EUR/USD", DXY: "DXY" };
const ORDER = ["CL", "BRENTOIL", "GOLD", "SILVER", "JPY", "EUR", "DXY", "GBP"];
const px = (p, ref = p) => (p == null ? "-" : p.toLocaleString(undefined, { maximumFractionDigits: ref >= 1000 ? 1 : ref >= 10 ? 2 : 4, minimumFractionDigits: ref >= 1000 ? 1 : ref >= 10 ? 2 : 4 }));
const pct = (v) => (v == null ? "-" : `${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v).toFixed(2)}%`);
const hourLabel = (ms) => new Date(ms).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit", hourCycle: "h23" });

function niceTicks(lo, hi, n = 3) {
  const raw = (hi - lo) / n;
  const p = 10 ** Math.floor(Math.log10(raw || 1));
  const step = [1, 2, 2.5, 5, 10].map((k) => k * p).find((s) => s >= raw) || raw;
  const out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi; v += step) out.push(Number(v.toPrecision(10)));
  return out;
}

function Chart({ m, c, days, onPick }) {
  const box = useRef(null);
  const [w, setW] = useState(220);
  const [hover, setHover] = useState(null);
  useEffect(() => {
    const el = box.current;
    if (!el || typeof ResizeObserver === "undefined") return undefined;
    const ro = new ResizeObserver(([e]) => setW(Math.max(160, Math.round(e.contentRect.width))));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  const series = c?.series?.[m.name] || [];
  const n = series.length;
  const from = Math.max(0, n - 1 - days * 24);
  const vals = series.slice(from);
  const ts = vals.map((_, k) => (from + k === n - 1 ? c.t_last : c.t0 + (from + k) * c.step));
  const prev = m.price != null && m.change_24h_pct != null ? m.price / (1 + m.change_24h_pct / 100) : null;
  const H = 118, R = 54, T = 6, B = 16;
  const pts = vals.map((v, k) => [ts[k], v]).filter(([, v]) => v != null);
  const up = m.change_24h_pct == null ? null : m.change_24h_pct >= 0;
  let body = <div className="cw-empty muted">No hourly history yet</div>;
  if (pts.length >= 3) {
    const all = pts.map(([, v]) => v).concat(prev != null ? [prev] : []);
    let lo = Math.min(...all), hi = Math.max(...all);
    const pad = (hi - lo || hi * 0.001) * 0.12;
    lo -= pad; hi += pad;
    const t0 = pts[0][0], t1 = pts.at(-1)[0];
    const X = (t) => 1 + ((t - t0) / (t1 - t0 || 1)) * (w - R - 2);
    const Y = (v) => T + (1 - (v - lo) / (hi - lo)) * (H - T - B);
    const line = pts.map(([t, v]) => `${X(t).toFixed(1)},${Y(v).toFixed(1)}`).join(" ");
    const area = `${X(t0)},${H - B} ${line} ${X(t1)},${H - B}`;
    // the real market's shut stretches inside the range
    const shut = [];
    let cur = t0;
    for (const [s, e] of c.open || []) {
      if (e <= t0 || s >= t1) continue;
      if (s > cur) shut.push([cur, Math.min(s, t1)]);
      cur = Math.max(cur, e);
    }
    if (cur < t1) shut.push([cur, t1]);
    const mids = [];
    const dt = new Date(t0);
    dt.setHours(24, 0, 0, 0);
    for (; dt.getTime() < t1; dt.setDate(dt.getDate() + 1)) mids.push(dt.getTime());
    const last = pts.at(-1);
    const hp = hover != null ? pts[hover] : null;
    const onMove = (e) => {
      const r = e.currentTarget.getBoundingClientRect();
      const t = t0 + ((e.clientX - r.left - 1) / (w - R - 2)) * (t1 - t0);
      let best = 0;
      pts.forEach(([pt], k) => { if (Math.abs(pt - t) < Math.abs(pts[best][0] - t)) best = k; });
      setHover(best);
    };
    body = (
      <svg width={w} height={H} viewBox={`0 0 ${w} ${H}`} onPointerMove={onMove} onPointerLeave={() => setHover(null)}>
        <defs>
          <linearGradient id={`cw-${m.ticker}`} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0" stopColor="#9fd9cf" stopOpacity="0.16" /><stop offset="1" stopColor="#9fd9cf" stopOpacity="0" />
          </linearGradient>
        </defs>
        {m.kind !== "fx" && shut.map(([s, e]) => <rect key={s} x={X(s)} y={T} width={Math.max(0, X(e) - X(s))} height={H - T - B} className="cw-shut" />)}
        {niceTicks(lo, hi).map((v) => (
          <g key={v}>
            <line x1={0} x2={w - R} y1={Y(v)} y2={Y(v)} className="cw-gl" />
            <text x={w - R + 4} y={Y(v) + 3} className="cw-ax">{px(v, m.price)}</text>
          </g>
        ))}
        {mids.map((t) => (
          <g key={t}>
            <line x1={X(t)} x2={X(t)} y1={T} y2={H - B} className="cw-gl" />
            {days <= 2 && <text x={X(t) + 2} y={H - 4} className="cw-ax">{new Date(t).toLocaleDateString([], { weekday: "short" })}</text>}
            {days > 2 && <text x={X(t) + 2} y={H - 4} className="cw-ax">{new Date(t).toLocaleDateString([], { weekday: "narrow" })}</text>}
          </g>
        ))}
        {prev != null && <line x1={0} x2={w - R} y1={Y(prev)} y2={Y(prev)} className="cw-prev" />}
        <polygon points={area} fill={`url(#cw-${m.ticker})`} />
        <polyline points={line} className="cw-line" />
        <circle cx={X(last[0])} cy={Y(last[1])} r="2.2" className="cw-dot" />
        <g transform={`translate(${w - R + 1} ${Y(last[1]) - 7})`}>
          <rect width={R - 2} height="14" rx="1" className={`cw-tag ${up == null ? "" : up ? "up" : "down"}`} />
          <text x={(R - 2) / 2} y="10" textAnchor="middle" className="cw-tag-text">{px(last[1], m.price)}</text>
        </g>
        {hp && (
          <g>
            <line x1={X(hp[0])} x2={X(hp[0])} y1={T} y2={H - B} className="cw-cross" />
            <circle cx={X(hp[0])} cy={Y(hp[1])} r="2.6" className="cw-cross-dot" />
          </g>
        )}
      </svg>
    );
    return (
      <button className="cw-cell" onClick={onPick} title={`${m.label}: open its row`}>
        <div className="cw-head">
          <b>{SHORT[m.ticker] || m.ticker}</b>
          <Flash value={m.price} className="cw-last num">{px(m.price)}</Flash>
          <span className={`cw-chg num ${up == null ? "" : up ? "up" : "down"}`}>{pct(m.change_24h_pct)}</span>
        </div>
        <div className="cw-sub">
          {hp ? <><span className="muted">{hourLabel(hp[0])}</span> <b className="num">{px(hp[1], m.price)}</b></>
            : <span className="muted">{m.label}{m.session && !m.session.open && m.session.why !== "break" ? " · HL alone" : ""}</span>}
        </div>
        <div ref={box} className="cw-plot">{body}</div>
      </button>
    );
  }
  return (
    <button className="cw-cell" onClick={onPick}>
      <div className="cw-head"><b>{SHORT[m.ticker] || m.ticker}</b><span className="cw-last num">{px(m.price)}</span></div>
      <div ref={box} className="cw-plot">{body}</div>
    </button>
  );
}

export default function Charts({ d, onPick }) {
  const [days, setDays] = useState(2);
  const all = [...d.markets, ...d.fx].filter((m) => m.ticker !== "DXY")           // the dollar index lives in FX
    .sort((a, b) => ORDER.indexOf(a.ticker) - ORDER.indexOf(b.ticker));
  const shut = d.markets.some((m) => m.session && !m.session.open && m.session.why !== "break");
  return (
    <section id="p-CHRT" className="panel term-wide">
      <Head code="CHRT" title="Chart wall"
        meta={[["Bars", "hourly"], ["Tag", "vs yesterday"], ["Shaded", "real market shut"]]}
        pills={[shut ? <Pill key="a" kind="state">HL alone</Pill> : null]}>
        <span className="tp-tabs" role="tablist">
          {[[2, "2 days"], [7, "7 days"]].map(([k, t]) => (
            <button key={k} role="tab" aria-selected={days === k} className={days === k ? "on" : ""} onClick={() => setDays(k)}>{t}</button>
          ))}
        </span>
      </Head>
      <div className="cw-grid">
        {all.map((m) => <Chart key={m.name} m={m} c={d.compare} days={days} onPick={() => onPick(m)} />)}
      </div>
    </section>
  );
}

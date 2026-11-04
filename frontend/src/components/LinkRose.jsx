import { useState } from "react";
import { Head } from "./TermPanel.jsx";
import { Measured } from "./Compare.jsx";
import { Seg, useFlow } from "./River.jsx";

/* Two panels side by side, data in one muted green and one muted red only.

   CNST, market links: the markets on a line with an arc over every pair that moved together (green) or opposite
   (red, dashed) over the window, thicker the stronger; each dot sized by its pace against normal and coloured by who
   is winning (net bought or sold) over 15 minutes. Under it, each strong pair's 24-hour rolling correlation through
   the window, with a verdict (forming, loosening, flipped, steady), the weekends shaded and scheduled moments marked.

   PRES, the pressure rose: one petal per market, its left half selling and its right half buying, each against the
   market's normal pace for this weekday and hour. Under it, all markets' buying and selling against normal over the
   last 6 hours.

   Pick a pair (hover an arc or a line, click to keep it) or a market (hover a dot): everything else steps back in
   both panels. */

const SHORT = { CL: "WTI", BRENTOIL: "BRENT", JPY: "USD/JPY", EUR: "EUR/USD", DXY: "DXY", GBP: "GBP/USD" };
const ARC_ORDER = ["CL", "BRENTOIL", "GOLD", "SILVER", "EUR", "DXY", "JPY", "GBP"];   // groups side by side
const ROSE_ORDER = ["CL", "BRENTOIL", "GOLD", "SILVER", "JPY", "EUR", "DXY", "GBP"];
const tk = (n) => n.split(":").pop();
const label = (n) => SHORT[tk(n)] || tk(n);
const TINY = { JPY: "JPY", EUR: "EUR", GBP: "GBP" };       // phone widths: the currency alone
const short = (n, narrow) => (narrow && TINY[tk(n)]) || label(n);
const G = "#4a9e7f", R = "#c4646b";
const LINK_MIN = 0.35;
const usd = (x) => {
  const a = Math.abs(x);
  return `${x > 0 ? "+" : x < 0 ? "−" : ""}${a >= 1e6 ? `$${(a / 1e6).toFixed(2)}M` : a >= 1e3 ? `$${(a / 1e3).toFixed(a >= 1e5 ? 0 : 1)}k` : `$${Math.round(a)}`}`;
};
const pct = (x) => (x == null ? "–" : `${x > 0 ? "+" : x < 0 ? "−" : ""}${Math.abs(x).toFixed(2)}%`);
const rr = (x) => `${x > 0 ? "+" : x < 0 ? "−" : ""}${Math.abs(x).toFixed(2)}`;
const fx = (x) => (x == null ? "–" : `${x.toFixed(x >= 10 ? 0 : 1)}×`);
const hhmm = (ms) => new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
const dayName = (ms) => new Date(ms).toLocaleDateString([], { weekday: "short" });
const byOrder = (order) => (a, b) => order.indexOf(tk(a)) - order.indexOf(tk(b));

/* Is this pair or market part of what's picked? */
const pairOn = (pick, a, b) => !pick || (pick.a ? (pick.a === a && pick.b === b) || (pick.a === b && pick.b === a) : pick.k === a || pick.k === b);
const marketOn = (pick, k) => !pick || (pick.a ? pick.a === k || pick.b === k : pick.k === k);
const samePick = (p, q) => p && q && p.a === q.a && p.b === q.b && p.k === q.k;

/* Each market's last 15 minutes from the shared flow poll, and its 1-hour move. */
function marketInfo(d, flow) {
  const rows = Object.fromEntries([...d.markets, ...d.fx].map((x) => [x.name, x]));
  const out = {};
  for (const m of flow?.markets || []) {
    const h = m.horizons?.[15];
    out[m.coin] = { pace: m.pace, share: h && h.b + h.s ? h.b / (h.b + h.s) : null, net: h ? h.b - h.s : null };
  }
  for (const [n, x] of Object.entries(rows)) out[n] = { ...(out[n] || {}), chg: x.change_1h_pct };
  return out;
}

function Arcs({ W, names, r, info, pick, hover, pin }) {
  const H = 196, base = 150;
  const x0 = W < 560 ? 34 : 60, x1 = W - x0;
  const X = (n) => x0 + (names.indexOf(n) / Math.max(1, names.length - 1)) * (x1 - x0);
  const arcs = [];
  names.forEach((a, i) => names.forEach((b, j) => {
    const v = r(a, b);
    if (j > i && v != null && Math.abs(v) >= LINK_MIN) arcs.push([a, b, v]);
  }));
  arcs.sort((p, q) => Math.abs(p[2]) - Math.abs(q[2]));
  return (
    <svg width={W} height={H} className="lk-arcs" role="img" aria-label="Arcs between markets that moved together (green) or opposite (red)">
      {arcs.map(([a, b, v]) => {
        const xa = X(a), xb = X(b), mx = (xa + xb) / 2, h = Math.min(base - 22, Math.abs(xb - xa) * 0.72);
        const d = `M${xa},${base - 12}Q${mx},${base - 12 - h * 1.6} ${xb},${base - 12}`;
        const on = pairOn(pick, a, b), k = Math.abs(v), picked = pick?.a && on;
        return (
          <g key={`${a}-${b}`} className="lk-arc" data-pair={`${tk(a)}-${tk(b)}`} onPointerEnter={() => hover({ a, b })} onPointerLeave={() => hover(null)}
            onClick={() => pin({ a, b })}>
            <path d={d} fill="none" stroke="transparent" strokeWidth="14" />
            <path d={d} fill="none" stroke={v > 0 ? G : R} strokeWidth={1 + 3 * k} strokeOpacity={(0.3 + 0.55 * k) * (on ? 1 : 0.16)}
              strokeDasharray={v < 0 ? "5 4" : undefined} strokeLinecap="round" />
            {(k >= 0.6 || picked) && on && (
              <g>
                <rect x={mx - 21} y={base - 12 - h * 0.8 - 9} width="42" height="15" rx="2" className="lk-chip" />
                <text x={mx} y={base - 12 - h * 0.8 + 2.5} textAnchor="middle" className="lk-v" fill={v > 0 ? G : R}>{rr(v)}</text>
              </g>
            )}
          </g>
        );
      })}
      <line x1={x0 - 26} x2={x1 + 26} y1={base + 0.5} y2={base + 0.5} className="lk-base" />
      {names.map((n) => {
        const x = X(n), m = info[n] || {}, rad = 6 + 4 * Math.sqrt(Math.min(m.pace ?? 0.6, 6));
        const c = (m.share ?? 0.5) >= 0.5 ? G : R;
        return (
          <g key={n} opacity={marketOn(pick, n) ? 1 : 0.32} className="lk-node" onPointerEnter={() => hover({ k: n })}
            onPointerLeave={() => hover(null)} onClick={() => pin({ k: n })}>
            <circle cx={x} cy={base - 12} r={Math.max(rad, 18)} fill="transparent" />
            <circle cx={x} cy={base - 12} r={rad} fill={c} fillOpacity="0.88" stroke="#050908" strokeWidth="1.5" />
            <text x={x} y={base + 18} textAnchor="middle" className="lk-name">{short(n, W < 560)}</text>
            <text x={x} y={base + 32} textAnchor="middle" className="lk-sub">
              <tspan fill={m.chg > 0 ? G : m.chg < 0 ? R : undefined}>{pct(m.chg)}</tspan>
              {W >= 560 && <tspan>{"  "}{fx(m.pace)}</tspan>}
            </text>
          </g>
        );
      })}
    </svg>
  );
}

function Lines({ W, L, days, pick, hover, pin }) {
  const [at, setAt] = useState(null);
  const wide = W >= 640;
  const x0 = 40, x1 = W - (wide ? 222 : 128), top = 40, ph = wide ? 236 : 180;
  const nm = (l) => `${short(l.a, !wide)}–${short(l.b, !wide)}`;
  const n = L.lines[0]?.series.length || days * 24;
  const T = (i) => L.t0 + i * L.step;
  const X = (t) => x0 + ((t - L.t0) / ((n - 1) * L.step)) * (x1 - x0);
  const Y = (v) => top + ((1 - v) / 2) * ph;
  const lastOf = (s) => { for (let i = s.length - 1; i >= 0; i--) if (s[i] != null) return s[i]; return 0; };
  const ends = L.lines.map((l) => ({ l, v: lastOf(l.series), y: Y(lastOf(l.series)) })).sort((a, b) => a.y - b.y);
  for (let i = 1; i < ends.length; i++) if (ends[i].y - ends[i - 1].y < 15) ends[i].y = ends[i - 1].y + 15;
  // day marks: local midnights for a week, Mondays for a month
  const ticks = [];
  const d0 = new Date(L.t0); d0.setHours(24, 0, 0, 0);
  for (let t = d0.getTime(); t < T(n - 1); t += 86_400_000) {
    const dt = new Date(t);
    if (days <= 7 || dt.getDay() === 1) ticks.push([t, days <= 7 ? dayName(t) : dt.toLocaleDateString([], { day: "numeric", month: "short" })]);
  }
  const evs = [];
  if (days <= 7) {
    let last = -1e9;
    for (const e of L.events) { const x = X(e.t_ms); if (x - last > 30) { evs.push({ ...e, x }); last = x; } }
  }
  const widest = [...L.shut].sort((a, b) => (b[1] - b[0]) - (a[1] - a[0]))[0];
  const pickedLine = pick?.a ? L.lines.find((l) => pairOn(pick, l.a, l.b)) : null;
  const path = (s) => {
    let d = "", pen = false;
    s.forEach((v, i) => {
      if (v == null) { pen = false; return; }
      d += `${pen ? "L" : "M"}${X(T(i)).toFixed(1)},${Y(v).toFixed(1)}`;
      pen = true;
    });
    return d;
  };
  const onMove = (e) => {
    const box = e.currentTarget.getBoundingClientRect();
    const mx = e.clientX - box.left;
    if (mx < x0 || mx > x1) return setAt(null);
    setAt(Math.max(0, Math.min(n - 1, Math.round(((mx - x0) / (x1 - x0)) * (n - 1)))));
  };
  const H = top + ph + 30;
  return (
    <div className="lk-lines" onPointerMove={onMove} onPointerLeave={() => setAt(null)}>
      <svg width={W} height={H} role="img" aria-label="Each strong pair's 24-hour rolling correlation through the window">
        <text x="12" y="16" className="lk-title">{wide ? `How strongly each pair moved together, through the ${days <= 7 ? "week" : "month"} (24-hour rolling)`
          : `Each pair's link through the ${days <= 7 ? "week" : "month"}`}</text>
        <rect x={x0} y={Y(1)} width={x1 - x0} height={ph / 2} fill={G} fillOpacity="0.035" />
        <rect x={x0} y={Y(0)} width={x1 - x0} height={ph / 2} fill={R} fillOpacity="0.035" />
        {L.shut.map(([a, b]) => {
          const xa = Math.max(x0, X(a)), xb = Math.min(x1, X(b));
          return xb > xa ? <rect key={a} x={xa} y={Y(1)} width={xb - xa} height={ph} className="lk-shut" /> : null;
        })}
        {widest && X(widest[1]) - X(widest[0]) > 70 && (
          <text x={(Math.max(x0, X(widest[0])) + Math.min(x1, X(widest[1]))) / 2} y={Y(-1) - 6} textAnchor="middle" className="lk-note">real markets shut</text>
        )}
        {[1, 0.5, 0, -0.5, -1].map((v) => (
          <g key={v}>
            <line x1={x0} x2={x1} y1={Math.round(Y(v)) + 0.5} y2={Math.round(Y(v)) + 0.5} className={v === 0 ? "lk-zero" : "lk-gl"} />
            <text x={x0 - 6} y={Y(v) + 3} textAnchor="end" className="lk-ax">{v > 0 ? "+" : v < 0 ? "−" : ""}{Math.abs(v)}</text>
          </g>
        ))}
        <text x={x0 + 6} y={Y(1) + 12} className="lk-zone g">Move together</text>
        <text x={x0 + 6} y={Y(-1) - 6} className="lk-zone r">Move opposite</text>
        {ticks.map(([t, txt]) => <text key={t} x={X(t) + 2} y={Y(-1) + 16} className="lk-ax">{txt}</text>)}
        {evs.map((e) => (
          <g key={`${e.t_ms}${e.label}`}>
            <line x1={Math.round(e.x) + 0.5} x2={Math.round(e.x) + 0.5} y1={Y(1)} y2={Y(-1)} className="lk-ev" />
            <text x={e.x} y={Y(1) - 5} textAnchor="middle" className="lk-note">{e.label}</text>
          </g>
        ))}
        {L.lines.map((l) => {
          const on = pairOn(pick, l.a, l.b), c = lastOf(l.series) > 0 ? G : R, d = path(l.series);
          return (
            <g key={`${l.a}${l.b}`} className="lk-line" onPointerEnter={() => hover({ a: l.a, b: l.b })} onPointerLeave={() => hover(null)}
              onClick={() => pin({ a: l.a, b: l.b })}>
              <path d={d} fill="none" stroke="transparent" strokeWidth="12" />
              <path d={d} fill="none" stroke={c} strokeWidth={pick?.a && on ? 2.2 : 1.6} strokeOpacity={on ? 0.92 : 0.15} strokeLinejoin="round" />
            </g>
          );
        })}
        {ends.map(({ l, v, y }) => {
          const on = pairOn(pick, l.a, l.b), c = v > 0 ? G : R;
          return (
            <g key={`e${l.a}${l.b}`} opacity={on ? 1 : 0.35} className="lk-line lk-endg" data-pair={`${tk(l.a)}-${tk(l.b)}`} onPointerEnter={() => hover({ a: l.a, b: l.b })}
              onPointerLeave={() => hover(null)} onClick={() => pin({ a: l.a, b: l.b })}>
              <circle cx={X(T(n - 1))} cy={Y(v)} r="3" fill={c} />
              <line x1={X(T(n - 1)) + 4} y1={Y(v)} x2={x1 + 12} y2={y} className="lk-lead" />
              <text x={x1 + 16} y={y + 4} className="lk-end">
                {nm(l)} <tspan className="lk-v" fill={c}>{rr(v)}</tspan>
                {wide && <tspan className="lk-verdict"> {l.verdict}</tspan>}
              </text>
            </g>
          );
        })}
        {pickedLine && (() => {
          const w = pickedLine.weakest, i = Math.round((w.t_ms - L.t0) / L.step);
          const bw = wide ? 272 : Math.min(240, x1 - x0 - 8), bx = Math.min(x1 - bw - 4, Math.max(x0 + 4, X(T(i)) + 14)), by = Y(1) + 6;
          return (
            <g pointerEvents="none">
              <rect x={bx} y={by} width={bw} height="58" rx="3" className="lk-call" />
              <text x={bx + 10} y={by + 17} className="lk-call-h">{label(pickedLine.a)}–{label(pickedLine.b)}</text>
              <text x={bx + bw - 10} y={by + 17} textAnchor="end" className="lk-v" fill={pickedLine.last > 0 ? G : R}>{rr(lastOf(pickedLine.series))} now</text>
              <text x={bx + 10} y={by + 34} className="lk-call-t">
                {pickedLine.r > 0 ? "Weakest" : "Least opposite"} <tspan className="lk-v lk-ink">{rr(w.r)}</tspan> {dayName(w.t_ms)} {hhmm(w.t_ms)}{w.shut ? ", real markets shut" : ""}
              </text>
              <text x={bx + 10} y={by + 49} className="lk-call-t">
                First day <tspan className="lk-v lk-ink">{rr(pickedLine.first)}</tspan>, last day <tspan className="lk-v lk-ink">{rr(pickedLine.last)}</tspan>: {pickedLine.verdict}
              </text>
            </g>
          );
        })()}
        {at != null && <line x1={X(T(at))} x2={X(T(at))} y1={Y(1)} y2={Y(-1)} className="lk-cross" />}
      </svg>
      {at != null && (
        <div className="lk-tip" style={{ left: X(T(at)), top: top + 8, transform: `translateX(${X(T(at)) > W * 0.55 ? "calc(-100% - 10px)" : "10px"})` }}>
          <div className="lk-tip-h">{dayName(T(at))} {hhmm(T(at))}</div>
          {L.lines.map((l) => (
            <div key={l.a + l.b} className={pairOn(pick, l.a, l.b) ? "" : "dim"}>
              <span>{nm(l)}</span>
              <b className="num" style={{ color: (l.series[at] ?? 0) > 0 ? G : R }}>{l.series[at] == null ? "–" : rr(l.series[at])}</b>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export default function MarketLinks({ d, flow, pick, hover, pin, pinned }) {
  const [days, setDays] = useState(7);
  const L = d.links?.[days];
  const info = marketInfo(d, flow);
  if (!L || !L.names.length) {
    return (
      <section id="p-CNST" className="panel lk-panel">
        <Head code="CNST" title="Market links" meta={[["Links", "hourly moves"]]} />
        <p className="plan-note muted term-pad">The links need a few days of hourly history.</p>
      </section>
    );
  }
  const names = [...L.names].sort(byOrder(ARC_ORDER));
  const idx = Object.fromEntries(L.names.map((n, i) => [n, i]));
  const r = (a, b) => L.m[idx[a]]?.[idx[b]] ?? null;
  const pairs = [];
  names.forEach((a, i) => names.forEach((b, j) => { if (j > i && r(a, b) != null) pairs.push([a, b, r(a, b)]); }));
  const together = [...pairs].sort((p, q) => q[2] - p[2])[0];
  const apart = [...pairs].sort((p, q) => p[2] - q[2])[0];
  const shown = pairs.filter((p) => Math.abs(p[2]) >= LINK_MIN).length;
  return (
    <section id="p-CNST" className="panel lk-panel">
      <Head code="CNST" title="Market links" meta={[["Links", "hourly moves"], ["Dots", "last 15 min"]]} />
      <div className="rv-bar">
        <div className="rv-kpis lk-kpis">
          <div><span>Strongest together</span><b>{together && together[2] > 0 ? <>{label(together[0])}–{label(together[1])} <small style={{ color: G }}>{rr(together[2])}</small></> : "–"}</b></div>
          <div><span>Strongest opposite</span><b>{apart && apart[2] < 0 ? <>{label(apart[0])}–{label(apart[1])} <small style={{ color: R }}>{rr(apart[2])}</small></> : "–"}</b></div>
          <div><span>{pinned ? "Picked" : "Links shown"}</span><b className="num">{pinned
            ? <>{pinned.a ? `${label(pinned.a)}–${label(pinned.b)}` : label(pinned.k)} <button className="lk-clear" onClick={() => pin(null)}>Clear</button></>
            : <>{shown} <small>of {pairs.length}</small></>}</b></div>
        </div>
        <Seg value={days} set={setDays} opts={[[7, "7d"], [30, "30d"]]} />
      </div>
      <Measured>{(w) => (
        <div className="lk-stage">
          <Arcs W={w - 22} names={names} r={r} info={info} pick={pick} hover={hover} pin={pin} />
          {L.lines.length > 0 && <Lines W={w - 22} L={L} days={days} pick={pick} hover={hover} pin={pin} />}
        </div>
      )}</Measured>
      <div className="lk-key">
        <span><i className="lk-k g" />Move together</span>
        <span><i className="lk-k r dash" />Move opposite</span>
        <span>Dot: net bought or sold over 15 min, size is pace</span>
        <span>Hover a pair to pick it, click to keep it</span>
      </div>
    </section>
  );
}

/* ── the pressure rose ──────────────────────────────────────────────── */
const sq = (x, RAD) => RAD * Math.sqrt(Math.min(Math.max(x || 0, 0), 4) / 4);   // area-true

function wedge(cx, cy, r0, r1, a0, a1) {
  const p = (a, r) => [cx + Math.cos(a) * r, cy + Math.sin(a) * r];
  const [a, b] = p(a0, r1), [c, d] = p(a1, r1), [e, f] = p(a1, r0), [g, h] = p(a0, r0);
  return `M${a},${b}A${r1},${r1} 0 0,1 ${c},${d}L${e},${f}A${r0},${r0} 0 0,0 ${g},${h}Z`;
}

function Rose({ W, rows, pick, total, hzName }) {
  const side = W >= 600;
  const H = side ? 372 : 330;
  const cx = side ? (W - 170) / 2 + 10 : W / 2, cy = H / 2 + 4;
  const RAD = Math.min(side ? 150 : W / 2 - 64, H / 2 - 36);
  const n = rows.length, seg = (2 * Math.PI) / n, gap = 0.05;
  const rx = W - 160;
  return (
    <svg width={W} height={H} className="rs-svg" role="img" aria-label="Each market's selling (left half) and buying (right half) against its normal pace">
      {[[0.5, "½×"], [1, "1×"], [2, "2×"], [4, "4×"]].map(([x, t]) => (
        <g key={x}>
          <circle cx={cx} cy={cy} r={sq(x, RAD)} className={`rs-ring ${x === 1 ? "norm" : ""}`} />
          <text x={cx} y={cy - sq(x, RAD) + 3.5} textAnchor="middle" className="rs-rl">{t}</text>
        </g>
      ))}
      {rows.map((m, i) => {
        const a0 = -Math.PI / 2 + i * seg + gap, a1 = -Math.PI / 2 + (i + 1) * seg - gap, am = (a0 + a1) / 2;
        const lr = Math.max(sq(m.buy, RAD), sq(m.sell, RAD), sq(1, RAD)) + 16, c = Math.cos(am), s = Math.sin(am);
        const lx = cx + c * lr, ly = cy + s * lr, anc = Math.abs(c) < 0.25 ? "middle" : c > 0 ? "start" : "end";
        const dy = s > 0.3 ? 8 : s < -0.3 ? -4 : 2;
        return (
          <g key={m.coin} opacity={marketOn(pick, m.coin) ? 1 : 0.28} className="rs-petal">
            <path d={wedge(cx, cy, 14, Math.max(16, sq(m.sell, RAD)), a0, am - 0.01)} fill={R} fillOpacity="0.74" />
            <path d={wedge(cx, cy, 14, Math.max(16, sq(m.buy, RAD)), am + 0.01, a1)} fill={G} fillOpacity="0.82" />
            <text x={lx} y={ly + dy} textAnchor={anc} className="rs-name">{label(m.coin)}</text>
            <text x={lx} y={ly + dy + 13} textAnchor={anc} className="rs-sub">
              <tspan fill={R}>{m.sell == null ? "–" : m.sell.toFixed(1)}</tspan> / <tspan fill={G}>{m.buy == null ? "–" : m.buy.toFixed(1)}</tspan>
            </text>
          </g>
        );
      })}
      <circle cx={cx} cy={cy} r="13" className="rs-hub" />
      {side && (
        <g>
          <text x={rx} y="40" className="rs-k">All markets, {hzName}</text>
          <text x={rx} y="68" className="rs-big">{fx(total.pace)}</text>
          <text x={rx + 54} y="68" className="rs-t">normal pace</text>
          <text x={rx} y="98" className="rs-t">{total.share == null ? "–" : `${Math.round(total.share * 100)}% bought`}</text>
          <rect x={rx} y="106" width="128" height="4" fill={R} />
          <rect x={rx} y="106" width={128 * (total.share ?? 0.5)} height="4" fill={G} />
          <text x={rx} y="144" className="rs-k">Each petal</text>
          <text x={rx} y="162" className="rs-t">Left half: selling</text>
          <text x={rx} y="178" className="rs-t">Right half: buying</text>
          <text x={rx} y="194" className="rs-t">Dashed ring: normal pace</text>
        </g>
      )}
    </svg>
  );
}

function SixHours({ W, day }) {
  const wide = W >= 600;
  const x0 = 42, x1 = W - (wide ? 120 : 84), top = 34, ph = 118;
  const step = day.step_ms;
  const k0 = Math.max(0, day.data_from_ms == null ? 0 : day.t.findIndex((t, k) => k === day.t.length - 1 || t - step / 2 >= day.data_from_ms));
  const t = day.t.slice(k0);
  const buy = [], sell = [], top1 = [];
  t.forEach((_, j) => {
    const k = j + k0;
    let B = 0, S = 0, N = 0, best = null;
    for (const m of day.markets) {
      const nm = m.norm[k];
      if (!nm) continue;
      B += m.b[k]; S += m.s[k]; N += nm;
      const bx = m.b[k] / (nm / 2), sx = m.s[k] / (nm / 2);
      if (!best || Math.max(bx, sx) > best[1]) best = [m.coin, Math.max(bx, sx), bx >= sx ? "buying" : "selling"];
    }
    buy.push(N ? B / (N / 2) : null);
    sell.push(N ? S / (N / 2) : null);
    top1.push(best);
  });
  if (t.length < 3 || buy.every((x) => x == null)) return <p className="plan-note muted term-pad">The last 6 hours fill in as trades come.</p>;
  const X = (ms) => x0 + ((ms - t[0]) / (t.at(-1) - t[0] || 1)) * (x1 - x0);
  const ymax = Math.max(3, Math.ceil(Math.max(...buy.filter(Boolean), ...sell.filter(Boolean)) * 1.1));
  const Y = (v) => top + ph - (Math.min(v, ymax) / ymax) * ph;
  const line = (v) => v.map((x, j) => (x == null ? "" : `${j && v[j - 1] != null ? "L" : "M"}${X(t[j]).toFixed(1)},${Y(x).toFixed(1)}`)).join("");
  const gaps = [];
  for (let j = 0; j < t.length - 1; j++) {
    if ([buy[j], buy[j + 1], sell[j], sell[j + 1]].some((x) => x == null)) continue;
    gaps.push(<path key={j} d={`M${X(t[j])},${Y(buy[j])}L${X(t[j + 1])},${Y(buy[j + 1])}L${X(t[j + 1])},${Y(sell[j + 1])}L${X(t[j])},${Y(sell[j])}Z`}
      fill={buy[j] >= sell[j] ? G : R} fillOpacity="0.16" />);
  }
  const peak = buy.concat(sell).reduce((bi, v, i, all) => (v != null && (bi < 0 || v > all[bi]) ? i : bi), -1);
  const pj = peak % t.length, pv = peak >= 0 ? (peak < t.length ? buy[pj] : sell[pj]) : null;
  const tb = top1[pj];
  let yb = Y(buy.at(-1) ?? 0) + 4, ys = Y(sell.at(-1) ?? 0) + 4;
  if (Math.abs(yb - ys) < 15) { const mid = (yb + ys) / 2, up = (buy.at(-1) ?? 0) >= (sell.at(-1) ?? 0); yb = mid + (up ? -8 : 8); ys = mid + (up ? 8 : -8); }
  const ticks = [];
  for (let h = 6; h >= 1; h -= wide ? 1 : 2) ticks.push([day.now_ms - h * 3_600_000, `−${h}h`]);
  const evs = (day.events || []).filter((e) => e.t_ms >= t[0]);
  return (
    <svg width={W} height={top + ph + 26} className="rs-day" role="img" aria-label="All markets' buying and selling against normal over the last 6 hours">
      <text x="12" y="16" className="lk-title">All markets, last 6 hours: buying and selling against normal</text>
      {Array.from({ length: ymax + 1 }, (_, v) => (
        <g key={v}>
          <line x1={x0} x2={x1} y1={Math.round(Y(v)) + 0.5} y2={Math.round(Y(v)) + 0.5} className={v === 1 ? "rs-norm" : "lk-gl"} />
          <text x={x0 - 6} y={Y(v) + 3} textAnchor="end" className="lk-ax">{v}×</text>
        </g>
      ))}
      {evs.map((e) => (
        <g key={e.t_ms + e.title}>
          <line x1={Math.round(X(e.t_ms)) + 0.5} x2={Math.round(X(e.t_ms)) + 0.5} y1={top} y2={top + ph} className="lk-ev" />
          <text x={X(e.t_ms) + 4} y={top + 10} className="lk-note">{e.title}</text>
        </g>
      ))}
      {gaps}
      <path d={line(sell)} fill="none" stroke={R} strokeWidth="1.5" strokeLinejoin="round" />
      <path d={line(buy)} fill="none" stroke={G} strokeWidth="1.6" strokeLinejoin="round" />
      {pv >= 1.8 && tb && !evs.some((e) => Math.abs(X(e.t_ms) - X(t[pj])) < 90) && (
        <text x={X(t[pj])} y={Y(pv) - 8} textAnchor="middle" className="lk-note">{label(tb[0])} {tb[2]}, {fx(tb[1])}</text>
      )}
      <text x={x1 + 8} y={yb} className="rs-end g">Buying <tspan className="lk-v">{fx(buy.at(-1))}</tspan></text>
      <text x={x1 + 8} y={ys} className="rs-end r">Selling <tspan className="lk-v">{fx(sell.at(-1))}</tspan></text>
      {ticks.map(([ms, txt]) => (ms >= t[0] ? <text key={txt} x={X(ms)} y={top + ph + 16} className="lk-ax">{txt}</text> : null))}
      <text x={x1} y={top + ph + 16} textAnchor="end" className="lk-ax lk-ink">Now</text>
    </svg>
  );
}

export function PressureRose({ flow, pick }) {
  const [hz, setHz] = useState(15);
  const day = useFlow(360, true);                           // its own 6-hour, 5-minute view
  const rows = (flow?.markets || []).map((x) => {
    const h = x.horizons?.[hz] || {};
    return { coin: x.coin, buy: h.buy_x, sell: h.sell_x, b: h.b || 0, s: h.s || 0, norm: x.norm?.at(-1) };
  }).sort((a, b) => byOrder(ROSE_ORDER)(a.coin, b.coin));
  const known = rows.filter((x) => x.buy != null);
  const topB = [...known].sort((a, b) => b.buy - a.buy)[0];
  const topS = [...known].sort((a, b) => b.sell - a.sell)[0];
  const hot = known.filter((x) => Math.max(x.buy, x.sell) >= 2).length;
  const hzName = { 5: "5 min", 15: "15 min", 60: "1 hour" }[hz];
  const withN = rows.filter((x) => x.norm);
  const sumB = rows.reduce((a, x) => a + x.b, 0), sumS = rows.reduce((a, x) => a + x.s, 0);
  const total = {
    pace: withN.length ? withN.reduce((a, x) => a + x.b + x.s, 0) / hz / withN.reduce((a, x) => a + x.norm, 0) : null,
    share: sumB + sumS ? sumB / (sumB + sumS) : null,
  };
  return (
    <section id="p-PRES" className="panel lk-panel">
      <Head code="PRES" title="Pressure rose" meta={[["Against", "normal for this hour"], ["Over", hzName]]} />
      <div className="rv-bar">
        <div className="rv-kpis lk-kpis">
          <div><span>Most buying</span><b>{topB ? <>{label(topB.coin)} <small style={{ color: G }}>{fx(topB.buy)}</small></> : "–"}</b></div>
          <div><span>Most selling</span><b>{topS ? <>{label(topS.coin)} <small style={{ color: R }}>{fx(topS.sell)}</small></> : "–"}</b></div>
          <div><span>Hot markets</span><b className="num">{known.length ? hot : "–"} <small>at 2× or more</small></b></div>
        </div>
        <Seg value={hz} set={setHz} opts={[[5, "5m"], [15, "15m"], [60, "1h"]]} />
      </div>
      {!flow ? <div className="rv-empty muted">Loading…</div>
        : !known.length ? <p className="plan-note muted term-pad">The rose needs each market's normal pace (the hourly candles) and some trades.</p>
        : (
          <Measured>{(w) => (
            <div className="lk-stage">
              <Rose W={w - 22} rows={rows} pick={pick} total={total} hzName={hzName} />
              {day && <div className="lk-split"><SixHours W={w - 22} day={day} /></div>}
            </div>
          )}</Measured>
        )}
      <div className="lk-key">
        <span><i className="lk-dot g" />Buying</span>
        <span><i className="lk-sq r" />Selling</span>
        <span>Against each market's normal for this weekday and hour</span>
      </div>
    </section>
  );
}

export { samePick };

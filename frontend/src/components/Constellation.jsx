import { useMemo, useState } from "react";
import { Head } from "./TermPanel.jsx";
import { Measured } from "./Compare.jsx";
import { Seg } from "./River.jsx";

/* Two views of the same moment, in green and red only.

   CNST, the market constellation: every market is a star, placed so markets that moved together over the week sit
   close (classical scaling of the hourly-return correlations) and joined by a line: green when they move together,
   red when they move opposite, thicker the stronger. A star's size is its pace against normal over the last 15
   minutes, its colour who is winning there: green when takers bought more, red when they sold more.

   PRES, the pressure radar: one spoke per market. The green shape is buying against that market's normal pace, the
   red shape selling, so a shape reaching out on one spoke is pressure building there. Rings at ½×, 1× (normal), 2×
   and 4×. */

const SHORT = { CL: "WTI", BRENTOIL: "BRENT", JPY: "USD/JPY", EUR: "EUR/USD", DXY: "DXY", GBP: "GBP/USD" };
const ORDER = ["CL", "BRENTOIL", "GOLD", "SILVER", "JPY", "EUR", "DXY", "GBP"];
const tk = (n) => n.split(":").pop();
const label = (n) => SHORT[tk(n)] || tk(n);
const G = "#4a9e7f", R = "#c4646b";                          // muted: data colour, not decoration
const usd = (x) => {
  const a = Math.abs(x);
  return a >= 1e6 ? `$${(a / 1e6).toFixed(a >= 1e7 ? 1 : 2)}M` : a >= 1e3 ? `$${(a / 1e3).toFixed(a >= 1e5 ? 0 : 1)}k` : `$${Math.round(a)}`;
};
const signed = (x) => `${x > 0 ? "+" : x < 0 ? "−" : ""}${usd(x)}`;
const pct = (x) => (x == null ? "–" : `${x > 0 ? "+" : x < 0 ? "−" : ""}${Math.abs(x).toFixed(2)}%`);
const byOrder = (a, b) => ORDER.indexOf(tk(a)) - ORDER.indexOf(tk(b));
const LINK_MIN = 0.35;                                        // weaker links aren't drawn (they'd only add clutter)

/* Classical multidimensional scaling of correlation distances (√(2(1 − r))) to two dimensions, then nudged apart so no
   two stars overlap. Signs are fixed (WTI on the left, gold above) so the picture doesn't flip between refreshes. */
function place(names, m, W, H) {
  const n = names.length;
  const r = (i, j) => (i === j ? 1 : m?.[i]?.[j] ?? 0);
  const D2 = names.map((_, i) => names.map((__, j) => 2 * (1 - r(i, j))));
  const rowM = D2.map((row) => row.reduce((a, b) => a + b, 0) / n);
  const all = rowM.reduce((a, b) => a + b, 0) / n;
  const B = D2.map((row, i) => row.map((v, j) => -0.5 * (v - rowM[i] - rowM[j] + all)));
  const vecs = [];
  for (let k = 0; k < 2; k++) {
    let v = names.map((_, i) => Math.sin(i * 1.7 + k * 2.3) + 0.5);
    let lam = 0;
    for (let it = 0; it < 120; it++) {
      let w = B.map((row) => row.reduce((a, b, j) => a + b * v[j], 0));
      for (const [u] of vecs) {                                 // deflate what's already found
        const d = w.reduce((a, b, i) => a + b * u[i], 0);
        w = w.map((x, i) => x - d * u[i]);
      }
      const norm = Math.hypot(...w) || 1;
      lam = norm;
      v = w.map((x) => x / norm);
    }
    vecs.push([v, lam]);
  }
  let pts = names.map((_, i) => [vecs[0][0][i] * Math.sqrt(vecs[0][1]), vecs[1][0][i] * Math.sqrt(vecs[1][1])]);
  const iW = names.findIndex((x) => tk(x) === "CL"), iG = names.findIndex((x) => tk(x) === "GOLD");
  if (iW >= 0 && pts[iW][0] > 0) pts = pts.map(([x, y]) => [-x, y]);
  if (iG >= 0 && pts[iG][1] > 0) pts = pts.map(([x, y]) => [x, -y]);
  const spread = Math.max(...pts.map(([x, y]) => Math.hypot(x, y)));
  if (!Number.isFinite(spread) || spread < 1e-6) {           // no links yet: a ring
    pts = names.map((_, i) => [Math.cos((i / n) * 2 * Math.PI - Math.PI / 2), Math.sin((i / n) * 2 * Math.PI - Math.PI / 2)]);
  }
  const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
  const [x0, x1, y0, y1] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
  const PX = Math.min(90, W * 0.13), PY = 46;
  let out = pts.map(([x, y]) => [PX + ((x - x0) / (x1 - x0 || 1)) * (W - 2 * PX), PY + ((y - y0) / (y1 - y0 || 1)) * (H - 2 * PY)]);
  // a star and its label take about 120 × 64 px: keep every pair at least that far apart (in that ellipse)
  const AX = Math.min(124, W / 4.6), AY = 66;
  for (let it = 0; it < 140; it++) {
    for (let i = 0; i < n; i++) {
      for (let j = i + 1; j < n; j++) {
        const dx = out[j][0] - out[i][0], dy = out[j][1] - out[i][1];
        const e = Math.hypot(dx / AX, dy / AY) || 0.01;
        if (e < 1) {
          const k = (1 - e) / 2 / e;
          out[i] = [out[i][0] - dx * k, out[i][1] - dy * k];
          out[j] = [out[j][0] + dx * k, out[j][1] + dy * k];
        }
      }
    }
    out = out.map(([x, y]) => [Math.max(64, Math.min(W - 64, x)), Math.max(PY * 0.7, Math.min(H - PY * 1.2, y))]);   // labels stay inside
  }
  return out;
}

const nodeR = (pace) => 4.5 + 3.2 * Math.sqrt(Math.min(pace ?? 0.6, 6));

function Sky({ W, H, names, m, info, hi, setHi }) {
  const P = useMemo(() => place(names, m, W, H), [names.join(), JSON.stringify(m), W, H]);
  const links = [];
  names.forEach((a, i) => names.forEach((b, j) => {
    const r = m?.[i]?.[j];
    if (j > i && r != null && Math.abs(r) >= LINK_MIN) links.push({ a, b, i, j, r });
  }));
  links.sort((x, y) => Math.abs(x.r) - Math.abs(y.r));        // strongest drawn last, on top
  const on = (n) => !hi || n === hi;
  // where each node's name plate sits, so a link's value chip can avoid it
  const plates = names.map((n, i) => {
    const core = nodeR(info[n]?.pace);
    return [P[i][0] - 64, P[i][1] - core - 8, P[i][0] + 64, P[i][1] + core + 42];
  });
  const free = (x, y, w2) => plates.every(([a, b, c, d]) => x + w2 < a || x - w2 > c || y + 9 < b || y - 9 > d);
  const spot = (i, j, w2) => {
    for (const t of [0.5, 0.4, 0.6, 0.3, 0.7]) {
      const x = P[i][0] + (P[j][0] - P[i][0]) * t, y = P[i][1] + (P[j][1] - P[i][1]) * t;
      if (free(x, y, w2)) return [x, y];
    }
    return null;
  };
  const h = hi ? info[hi] : null;
  const hiLinks = hi ? links.filter((l) => l.a === hi || l.b === hi).sort((x, y) => Math.abs(y.r) - Math.abs(x.r)).slice(0, 3) : [];
  return (
    <div className="cn-wrap" style={{ height: H }}>
      <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} className="cn-sky" role="img"
        aria-label="Markets placed by how they moved together this week; lines green together, red opposite">
        {links.map(({ a, b, i, j, r }) => {
          const lit = !hi || a === hi || b === hi;
          const k = Math.abs(r);
          const txt = `${r > 0 ? "+" : "−"}${k.toFixed(2)}`;
          const at = (k >= 0.6 || (hi && lit)) ? spot(i, j, txt.length * 3.2 + 4) : null;
          const [mx, my] = at || [0, 0];
          return (
            <g key={`${a}-${b}`} className="cn-link" opacity={lit ? 1 : 0.15}>
              <line x1={P[i][0]} y1={P[i][1]} x2={P[j][0]} y2={P[j][1]} stroke={r > 0 ? G : R}
                strokeWidth={0.75 + 1.6 * k} strokeOpacity={0.3 + 0.55 * k} strokeDasharray={r < 0 ? "4 3" : undefined} />
              {at && (
                <g>
                  <rect x={mx - txt.length * 3.2 - 4} y={my - 8} width={txt.length * 6.4 + 8} height={15} rx="2" className="cn-chip" />
                  <text x={mx} y={my + 3.5} className="cn-r" fill={r > 0 ? G : R} textAnchor="middle">{txt}</text>
                </g>
              )}
            </g>
          );
        })}
        {names.map((n, i) => {
          const x = info[n] || {};
          const buy = (x.share ?? 0.5) >= 0.5;
          const core = nodeR(x.pace);
          const c = buy ? G : R;
          const [px, py] = P[i];
          const name = label(n);
          const sub = `${pct(x.chg)}  ${x.pace == null ? "–" : `${x.pace.toFixed(1)}×`}`;
          const w = Math.max(name.length * 7.2, sub.length * 6.3) + 12;
          return (
            <g key={n} className="cn-node" opacity={on(n) ? 1 : 0.3} onPointerEnter={() => setHi(n)} onPointerLeave={() => setHi(null)}>
              <circle cx={px} cy={py} r={Math.max(core * 2.2, 24)} fill="transparent" />
              {(x.pace ?? 0) >= 2 && <circle cx={px} cy={py} r={core + 4.5} fill="none" stroke={c} strokeOpacity="0.55" strokeWidth="1" />}
              <circle cx={px} cy={py} r={core} fill={c} fillOpacity={0.9} stroke="#050908" strokeWidth="1.5" />
              <rect x={px - w / 2} y={py + core + 5} width={w} height={33} rx="2" className="cn-plate" />
              <text x={px} y={py + core + 19} textAnchor="middle" className="cn-name">{name}</text>
              <text x={px} y={py + core + 32} textAnchor="middle" className="cn-sub">
                <tspan fill={x.chg > 0 ? G : x.chg < 0 ? R : undefined}>{pct(x.chg)}</tspan>
                <tspan className="cn-dim">{"  "}{x.pace == null ? "–" : `${x.pace.toFixed(1)}×`}</tspan>
              </text>
            </g>
          );
        })}
      </svg>
      {h && (
        <div className="cn-tip">
          <div className="cn-tip-h">{label(hi)}</div>
          <dl>
            <dt>1h</dt><dd className="num" style={{ color: h.chg > 0 ? G : h.chg < 0 ? R : undefined }}>{pct(h.chg)}</dd>
            <dt>Pace, 15m</dt><dd className="num">{h.pace == null ? "–" : `${h.pace.toFixed(2)}×`}</dd>
            <dt>Net, 15m</dt><dd className="num" style={{ color: h.net > 0 ? G : h.net < 0 ? R : undefined }}>{h.net == null ? "–" : signed(h.net)}</dd>
            <dt>Bought</dt><dd className="num">{h.share == null ? "–" : `${Math.round(h.share * 100)}%`}</dd>
          </dl>
          {hiLinks.length > 0 && (
            <div className="cn-tip-l">
              {hiLinks.map((l) => (
                <div key={l.a + l.b}><span>{label(l.a === hi ? l.b : l.a)}</span><b className="num" style={{ color: l.r > 0 ? G : R }}>{l.r > 0 ? "+" : "−"}{Math.abs(l.r).toFixed(2)}</b></div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export default function Constellation({ d, flow }) {
  const [hi, setHi] = useState(null);
  const c = d.compare?.corr;
  const names = c ? [...c.names].sort(byOrder) : [];
  const idx = c ? names.map((n) => c.names.indexOf(n)) : [];
  const m = c ? idx.map((i) => idx.map((j) => c.m[i][j])) : null;
  const rows = Object.fromEntries([...d.markets, ...d.fx].map((x) => [x.name, x]));
  const fm = Object.fromEntries((flow?.markets || []).map((x) => [x.coin, x]));
  const info = Object.fromEntries(names.map((n) => {
    const h = fm[n]?.horizons?.[15];
    const share = h && h.b + h.s ? h.b / (h.b + h.s) : null;
    return [n, { chg: rows[n]?.change_1h_pct, pace: fm[n]?.pace ?? null, share, net: h ? h.b - h.s : null }];
  }));
  const pairs = [];
  names.forEach((a, i) => names.forEach((b, j) => { if (j > i && m[i][j] != null) pairs.push([a, b, m[i][j]]); }));
  const together = [...pairs].sort((p, q) => q[2] - p[2])[0];
  const apart = [...pairs].sort((p, q) => p[2] - q[2])[0];
  const shown = pairs.filter((p) => Math.abs(p[2]) >= LINK_MIN).length;
  return (
    <section id="p-CNST" className="panel cn-panel">
      <Head code="CNST" title="Market constellation" meta={[["Links", "7 days, hourly"], ["Nodes", "last 15 min"]]}
        pills={[<span key="n" className="cn-badge">{shown} links</span>]} />
      <div className="rv-bar cn-bar">
        <div className="rv-kpis cn-kpis">
          <div><span>Strongest together</span><b>{together ? <>{label(together[0])}–{label(together[1])} <small style={{ color: G }}>+{together[2].toFixed(2)}</small></> : "–"}</b></div>
          <div><span>Strongest opposite</span><b>{apart && apart[2] < 0 ? <>{label(apart[0])}–{label(apart[1])} <small style={{ color: R }}>−{Math.abs(apart[2]).toFixed(2)}</small></> : "–"}</b></div>
          <div><span>Links shown</span><b className="num">{shown} <small>of {pairs.length}, |r| ≥ {LINK_MIN}</small></b></div>
        </div>
      </div>
      {!names.length ? <p className="plan-note muted term-pad">The constellation needs a few days of hourly history.</p>
        : <Measured>{(w) => <div className="cn-stage"><Sky W={w - 22} H={w < 640 ? 330 : 380} names={names} m={m} info={info} hi={hi} setHi={setHi} /></div>}</Measured>}
      <div className="cn-key">
        <span><i className="cn-k g" />Move together</span>
        <span><i className="cn-k r dash" />Move opposite</span>
        <span><i className="cn-dot g" />Net bought</span>
        <span><i className="cn-dot r" />Net sold</span>
        <span className="cn-size">Size: pace vs normal
          <svg width="74" height="18" aria-hidden="true">
            {[0.5, 1, 2].map((p, k) => <circle key={p} cx={8 + k * 24 + (k ? 2 : 0)} cy={9} r={nodeR(p) * 0.82} className="cn-size-c" />)}
          </svg>
          <em className="num">½× 1× 2×</em>
        </span>
      </div>
    </section>
  );
}

/* ── the pressure radar ───────────────────────────────────────────────── */
const RINGS = [0.5, 1, 2, 4];
const MAXX = 4;
const rad = (x, R) => R * Math.sqrt(Math.min(Math.max(x || 0, 0), MAXX) / MAXX);   // area-true: √ scale

function Scope({ W, H, rows, hz, hi, setHi }) {
  const cx = W / 2, cy = H / 2;
  const RAD = Math.min(W / 2 - 76, H / 2 - 34);
  const n = rows.length;
  const ang = (i) => -Math.PI / 2 + (i / n) * 2 * Math.PI;
  const at = (i, r) => [cx + Math.cos(ang(i)) * r, cy + Math.sin(ang(i)) * r];
  const poly = (key) => rows.map((x, i) => at(i, rad(x[key], RAD)).map((v) => v.toFixed(1)).join(",")).join(" ");
  const mid = -Math.PI / 2 + Math.PI / n;                     // ring labels sit between the first two spokes
  return (
    <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} className="pr-scope" role="img"
      aria-label={`Buying (green) and selling (red) against each market's normal pace over ${hz}`}>
      {RINGS.map((x) => (
        <g key={x}>
          <circle cx={cx} cy={cy} r={rad(x, RAD)} className={`pr-ring ${x === 1 ? "norm" : ""}`} />
          <text x={cx + Math.cos(mid) * rad(x, RAD) + 4} y={cy + Math.sin(mid) * rad(x, RAD) - 3} className="pr-rl">
            {x === 1 ? "Normal" : x === 0.5 ? "½×" : `${x}×`}
          </text>
        </g>
      ))}
      {rows.map((x, i) => {
        const [ex, ey] = at(i, RAD);
        return <line key={i} x1={cx} y1={cy} x2={ex} y2={ey} className={`pr-spoke ${hi === x.coin ? "on" : ""}`} />;
      })}
      <polygon points={poly("sell")} className="pr-poly sell" style={{ stroke: R, fill: R }} />
      <polygon points={poly("buy")} className="pr-poly buy" style={{ stroke: G, fill: G }} />
      {rows.map((x, i) => {
        const [bx, by] = at(i, rad(x.buy, RAD));
        const [sx, sy] = at(i, rad(x.sell, RAD));
        const big = hi === x.coin ? 3.6 : 2.4;
        return (
          <g key={x.coin}>
            <rect x={sx - big} y={sy - big} width={big * 2} height={big * 2} fill={R} stroke="#050908" strokeWidth="1" />
            <circle cx={bx} cy={by} r={big + 0.3} fill={G} stroke="#050908" strokeWidth="1" />
          </g>
        );
      })}
      {rows.map((x, i) => {
        const [lx, ly] = at(i, RAD + 16);
        const a = Math.cos(ang(i)), b = Math.sin(ang(i));
        const anchor = Math.abs(a) < 0.25 ? "middle" : a > 0 ? "start" : "end";
        const hot = Math.max(x.buy || 0, x.sell || 0) >= 2;
        return (
          <text key={`l-${x.coin}`} x={lx} y={ly + (b > 0.3 ? 10 : b < -0.3 ? -2 : 4)} textAnchor={anchor}
            className={`pr-name ${hi === x.coin ? "on" : ""} ${hot ? "hot" : ""}`}
            onPointerEnter={() => setHi(x.coin)} onPointerLeave={() => setHi(null)}>{label(x.coin)}</text>
        );
      })}
    </svg>
  );
}

const fmtX = (x) => (x == null ? "–" : `${x.toFixed(x >= 10 ? 0 : 1)}×`);

export function Radar({ flow }) {
  const [hz, setHz] = useState(15);
  const [hi, setHi] = useState(null);
  const rows = (flow?.markets || []).map((x) => {
    const h = x.horizons?.[hz] || {};
    return { coin: x.coin, buy: h.buy_x, sell: h.sell_x, b: h.b || 0, s: h.s || 0 };
  }).sort((a, b) => byOrder(a.coin, b.coin));
  const known = rows.filter((x) => x.buy != null);
  const topB = [...known].sort((a, b) => b.buy - a.buy)[0];
  const topS = [...known].sort((a, b) => b.sell - a.sell)[0];
  const hot = known.filter((x) => Math.max(x.buy, x.sell) >= 2).length;
  const hzName = { 5: "5 min", 15: "15 min", 60: "1 hour" }[hz];
  return (
    <section id="p-PRES" className="panel pr-panel">
      <Head code="PRES" title="Pressure radar" meta={[["Against", "normal for this hour"], ["Over", hzName]]}
        pills={[<span key="h" className={`cn-badge ${hot ? "hot" : ""}`}>{hot ? `${hot} at 2×+` : "none at 2×"}</span>]} />
      <div className="rv-bar cn-bar">
        <div className="rv-kpis cn-kpis">
          <div><span>Most buying</span><b>{topB ? <>{label(topB.coin)} <small style={{ color: G }}>{fmtX(topB.buy)}</small></> : "–"}</b></div>
          <div><span>Most selling</span><b>{topS ? <>{label(topS.coin)} <small style={{ color: R }}>{fmtX(topS.sell)}</small></> : "–"}</b></div>
        </div>
        <Seg value={hz} set={setHz} opts={[[5, "5m"], [15, "15m"], [60, "1h"]]} />
      </div>
      {!flow ? <div className="rv-empty muted">Loading…</div>
        : !known.length ? <p className="plan-note muted term-pad">The radar needs each market's normal pace (the hourly candles) and some trades.</p>
        : (
          <Measured>{(w) => {
            const side = w >= 600;
            const sw = side ? w - 22 - 252 : w - 22;
            return (
              <div className={`cn-stage pr-body ${side ? "side" : ""}`}>
                <Scope W={sw} H={side ? 380 : Math.min(340, sw)} rows={rows} hz={hzName} hi={hi} setHi={setHi} />
                <table className="pr-table">
                  <thead><tr><th>Market</th><th>Buy</th><th>Sell</th><th>Net</th></tr></thead>
                  <tbody>
                    {rows.map((x) => {
                      const net = x.b - x.s;
                      return (
                        <tr key={x.coin} className={hi === x.coin ? "on" : ""} onPointerEnter={() => setHi(x.coin)} onPointerLeave={() => setHi(null)}>
                          <td>{label(x.coin)}</td>
                          <td className="num" style={{ color: x.buy >= 2 ? G : undefined }}>{fmtX(x.buy)}</td>
                          <td className="num" style={{ color: x.sell >= 2 ? R : undefined }}>{fmtX(x.sell)}</td>
                          <td className="num" style={{ color: net > 0 ? G : net < 0 ? R : undefined }}>{x.b + x.s ? signed(net) : "–"}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            );
          }}</Measured>
        )}
      <div className="cn-key">
        <span><i className="cn-dot g" />Buying</span>
        <span><i className="cn-sq r" />Selling</span>
        <span title="Median of 8 weeks of hourly volume at this weekday and hour">vs normal pace for this weekday and hour · √ scale</span>
      </div>
    </section>
  );
}

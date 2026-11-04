import { useEffect, useId, useRef, useState } from "react";
import { fmtPct, fmtUsd } from "../format.js";
import { ChangeLog } from "./TermChrome.jsx";
import { useOrders } from "./Compare.jsx";
import Flow, { useFlow } from "./River.jsx";
import Constellation, { Radar } from "./Constellation.jsx";
import World from "./World.jsx";
import Charts from "./Charts.jsx";
import { Flash, Head, Pill, SourcePill, fresh } from "./TermPanel.jsx";

export { Flash };

/* The edge terminal's main view (/api/edges): trade.xyz's commodities and currencies on Hyperliquid, the weekend
   record, the oil roll, what's coming up and the board of edges being worked on. Times are in your time zone. */

const px = (p) => (p == null ? "-" : p.toLocaleString(undefined, { maximumFractionDigits: p >= 1000 ? 1 : p >= 10 ? 2 : 4, minimumFractionDigits: p >= 1000 ? 1 : p >= 10 ? 2 : 4 }));
const bps = (x, d = 0) => {
  if (x == null) return "-";
  const r = Number(x.toFixed(d));                         // no "−0 bps"
  return `${r > 0 ? "+" : r < 0 ? "−" : ""}${Math.abs(r).toFixed(d)} bps`;
};
const tone = (x) => (x == null || Math.abs(x) < 1e-9 ? "" : x > 0 ? "up" : "down");
const clock = (ms) => new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
const dayTime = (ms) => new Date(ms).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
const dayLabel = (ms) => new Date(ms).toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" });
const isoDay = (s) => new Date(`${s}T12:00:00`).toLocaleDateString([], { weekday: "short", day: "numeric" });
// A New York calendar day (the roll's business days), named without shifting it into another time zone
const isoDayLong = (ms) => new Date(ms).toLocaleDateString([], { weekday: "short", day: "numeric", month: "short", timeZone: "America/New_York" });
const span = (ms) => {
  const m = Math.max(0, ms / 60000);
  if (m < 60) return `${Math.round(m)}m`;
  const h = Math.floor(m / 60);
  return h < 48 ? `${h}h ${String(Math.round(m % 60)).padStart(2, "0")}m` : `${Math.floor(h / 24)}d ${h % 24}h`;
};
const pad2 = (n) => String(n).padStart(2, "0");
/** A ticking countdown: "06:21:13", or "2d 06:21:13" past a day. */
const hms = (ms) => {
  const t = Math.max(0, Math.floor(ms / 1000));
  const d = Math.floor(t / 86400);
  const core = `${pad2(Math.floor((t % 86400) / 3600))}:${pad2(Math.floor((t % 3600) / 60))}:${pad2(t % 60)}`;
  return d ? `${d}d ${core}` : core;
};
const SHORT = { CL: "WTI", BRENTOIL: "BRENT" };
const short = (m) => (m.kind === "fx" ? m.label : SHORT[m.ticker] || m.ticker);
const SHAPE = { CL: "circle", BRENTOIL: "square", GOLD: "triangle", SILVER: "diamond" };
const GROUP = { oil: "Oil", macro: "Macro", session: "Market hours", crypto: "Crypto" };
const STATUS = { testing: "Testing", watching: "Watching", planned: "Planned" };

/* The last 48 hours, hourly, ending at the live price. */
function Spark({ v, w = 88, h = 22 }) {
  const id = useId();
  const pts = (v || []).filter((x) => x != null);
  if (pts.length < 3) return <span className="muted">-</span>;
  const lo = Math.min(...pts);
  const r = Math.max(...pts) - lo || 1;
  const xy = pts.map((p, i) => [2 + (i / (pts.length - 1)) * (w - 6), h - 3 - ((p - lo) / r) * (h - 6)]);
  const line = xy.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
  const up = pts.at(-1) >= pts[0];
  return (
    <svg className={`term-spark ${up ? "up" : "down"}`} width={w} height={h} viewBox={`0 0 ${w} ${h}`}
      role="img" aria-label={`48 hours: ${up ? "up" : "down"} ${fmtPct((pts.at(-1) / pts[0] - 1) * 100)}`}>
      <defs>
        <linearGradient id={id} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" className="term-spark-top" />
          <stop offset="1" className="term-spark-bottom" />
        </linearGradient>
      </defs>
      <polygon points={`${xy[0][0]},${h} ${line} ${xy.at(-1)[0]},${h}`} fill={`url(#${id})`} />
      <polyline points={line} />
      <circle cx={xy.at(-1)[0]} cy={xy.at(-1)[1]} r="2" />
    </svg>
  );
}

/* Scrolling prices across the top of every tab; hover to stop it. Marks the markets Hyperliquid is pricing alone. */
export function Tape({ d }) {
  const items = [...(d?.markets || []), ...(d?.fx || [])].filter((m) => m.price != null);
  if (!items.length) return null;
  const row = items.map((m) => (
    <span key={m.name} className="term-tape-item">
      <b>{short(m)}</b>
      <Flash value={m.price} className="num">{px(m.price)}</Flash>
      <span className={tone(m.change_24h_pct)}>{m.change_24h_pct == null ? "" : m.change_24h_pct >= 0 ? "▲" : "▼"} {fmtPct(m.change_24h_pct)}</span>
      {m.session && !m.session.open && m.session.why !== "break" && <span className="term-tape-alone">HL only</span>}
    </span>
  ));
  return (
    <div className="term-tape" aria-label="Prices">
      <div className="term-tape-track">
        <div className="term-tape-group">{row}</div>
        <div className="term-tape-group" aria-hidden="true">{row}</div>
      </div>
    </div>
  );
}

function Shape({ k, x, y, className }) {
  if (k === "square") return <rect x={x - 2.4} y={y - 2.4} width="4.8" height="4.8" className={className} />;
  if (k === "triangle") return <polygon points={`${x},${y - 3.2} ${x + 3},${y + 2.4} ${x - 3},${y + 2.4}`} className={className} />;
  if (k === "diamond") return <polygon points={`${x},${y - 3.3} ${x + 3.3},${y} ${x},${y + 3.3} ${x - 3.3},${y}`} className={className} />;
  return <circle cx={x} cy={y} r="2.7" className={className} />;
}

function RealMarket({ m, now }) {
  const s = m.session;
  if (!s) return <span className="muted">-</span>;
  if (s.open) {
    return (
      <span title={`Shuts for the weekend in ${span(s.next_long_close_ms - now)}`}>
        <span className="term-dot open" />Open <span className="muted">· shuts {dayTime(s.next_long_close_ms)}</span>
      </span>
    );
  }
  if (s.why === "break") return <span><span className="term-dot" />Daily break <span className="muted">· back {clock(s.next_change_ms)}</span></span>;
  return (
    <span title={`Reopens ${dayTime(s.next_change_ms)}`}>
      <span className="term-dot shut" />Shut <span className="muted">· reopens in {span(s.next_change_ms - now)}</span>
    </span>
  );
}

function Markets({ d, now }) {
  const f = fresh(d, "Hyperliquid xyz", now, 60_000);
  const alone = d.markets.filter((m) => m.session && !m.session.open && m.session.why !== "break").length;
  return (
    <section id="p-CMDY" className="panel term-wide">
      <Head code="CMDY" title="Commodities on Hyperliquid"
        meta={[["Venue", "trade.xyz"], ["Fees", `${d.fees.taker_bps} / ${d.fees.maker_bps} bps`], ["Updated", f.age]]}
        pills={[alone ? <Pill key="a" kind="state" title="The real market is shut: Hyperliquid's book sets these prices">HL only · {alone}</Pill> : null,
          <SourcePill key="s" f={f} />]} />
      <div className="term-scroll">
        <table className="plan-table term-table">
          <thead>
            <tr>
              <th>Market</th><th>Real market</th><th>Price</th><th>24h</th><th>48h</th>
              <th title="Hyperliquid's price against trade.xyz's oracle (the real market's price while it's open)">vs oracle</th>
              <th>Funding / yr</th><th>Open interest</th><th>24h volume</th>
            </tr>
          </thead>
          <tbody>
            {d.markets.map((m) => [
              <tr key={m.name} data-mk={m.ticker}>
                <td><b>{m.label}</b> <span className="muted">{short(m)}</span></td>
                <td><RealMarket m={m} now={d.now_ms} /></td>
                <td className="num"><Flash value={m.price}>{px(m.price)}</Flash></td>
                <td className={`num ${tone(m.change_24h_pct)}`}>{fmtPct(m.change_24h_pct)}</td>
                <td><Spark v={m.spark} /></td>
                <td className="num">{bps(m.basis_bps, 1)}</td>
                <td className="num">{m.funding_apr == null ? "-" : fmtPct(m.funding_apr, 1)}</td>
                <td className="num">{fmtUsd(m.oi_usd)}</td>
                <td className="num">{fmtUsd(m.day_volume)}</td>
              </tr>,
              m.closed && (
                <tr key={`${m.name}-closed`} className="term-closed">
                  <td colSpan={9}>
                    <ClosedLine c={m.closed} />
                  </td>
                </tr>
              ),
            ])}
          </tbody>
        </table>
      </div>
      {d.markets.some((m) => m.closed) && (
        <div className="term-closed-list">
          {d.markets.filter((m) => m.closed).map((m) => (
            <p key={m.name}><b>{m.label}</b> · <ClosedLine c={m.closed} /></p>
          ))}
        </div>
      )}
      {!d.markets.some((m) => m.price != null) && (
        <p className="plan-note muted term-pad">{d.enabled ? "Waiting for the first prices…" : "Prices are read in live mode only; the hours and the schedule below still apply."}</p>
      )}
    </section>
  );
}

function ClosedLine({ c }) {
  if (!c.ref) return <span className="muted">Shut: Friday's close isn't in the hourly history yet.</span>;
  const e = c.expect;
  return (
    <span>
      Since the real market shut at <b>{px(c.ref)}</b>: <b className={tone(c.w_bps)}>{bps(c.w_bps)}</b>
      <span className="muted"> · allowed {px(c.band[0])} to {px(c.band[1])} (±{c.bound_pct}%)</span>
      {e && (
        <>
          {" · "}past weekends say the reopen moves about <b className={tone(e.snap_bps)}>{bps(e.snap_bps)}</b> from here
          <span className="muted"> ({e.from === "ours" ? "our record" : "published study"})</span>
        </>
      )}
    </span>
  );
}

function Scatter({ rows, fit, live }) {
  const pts = rows.filter((r) => r.w_bps != null && r.r_bps != null);
  if (pts.length < 3) return null;
  const lim = Math.max(50, ...pts.map((r) => Math.max(Math.abs(r.w_bps), Math.abs(r.r_bps))),
    ...live.map((l) => Math.abs(l.w) * 1.05)) * 1.1;
  const W = 260, H = 190, pad = 26;
  const x = (v) => pad + ((v + lim) / (2 * lim)) * (W - 2 * pad);
  const y = (v) => H - pad - ((v + lim) / (2 * lim)) * (H - 2 * pad);
  const kinds = [...new Set(pts.map((r) => r.tk))];
  return (
    <div>
      <svg className="term-scatter" viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Weekend move against the move at the reopen">
        <line x1={x(-lim)} y1={y(0)} x2={x(lim)} y2={y(0)} className="term-axis" />
        <line x1={x(0)} y1={y(-lim)} x2={x(0)} y2={y(lim)} className="term-axis" />
        <line x1={x(-lim)} y1={y(-lim)} x2={x(lim)} y2={y(lim)} className="term-same" />
        {fit && <line x1={x(-lim)} y1={y(fit.a - fit.b * lim)} x2={x(lim)} y2={y(fit.a + fit.b * lim)} className="term-fit" />}
        {pts.map((r) => (
          <g key={`${r.tk}-${r.close_ms}`}>
            <Shape k={SHAPE[r.tk]} x={x(r.w_bps)} y={y(r.r_bps)} className="term-pt" />
            <title>{`${r.market} · ${dayLabel(r.close_ms)}: weekend ${bps(r.w_bps)}, reopen ${bps(r.r_bps)}`}</title>
          </g>
        ))}
        {live.map((l) => (
          <g key={l.tk}>
            <circle cx={x(l.w)} cy={y(l.w + l.snap)} r="5" className="term-live" />
            <Shape k={SHAPE[l.tk]} x={x(l.w)} y={y(l.w + l.snap)} className="term-live-pt" />
            <title>{`${l.label} this weekend so far: ${bps(l.w)}; expected at the reopen ${bps(l.w + l.snap)}`}</title>
          </g>
        ))}
        <text x={W - pad} y={y(0) - 4} textAnchor="end" className="term-ax-label">weekend move</text>
        <text x={x(0) + 4} y={pad - 8} className="term-ax-label">at the reopen</text>
      </svg>
      <div className="term-legend">
        {kinds.map((k) => (
          <span key={k}>
            <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true"><Shape k={SHAPE[k]} x={5} y={5} className="term-pt" /></svg>
            {pts.find((r) => r.tk === k)?.market}
          </span>
        ))}
        {live.length > 0 && <span><i className="term-legend-live" />this weekend</span>}
        <span><i className="term-legend-fit" />fit</span>
        <span><i className="term-legend-same" />kept it all</span>
      </div>
    </div>
  );
}

function Record({ d }) {
  const w = d.weekend;
  const p = w.pooled;
  const label = Object.fromEntries(d.markets.map((m) => [m.name, m.label]));
  const tick = Object.fromEntries(d.markets.map((m) => [m.name, m.ticker]));
  const all = Object.entries(w.markets).flatMap(([name, x]) => x.rows.map((r) => ({ ...r, market: label[name] || name, tk: tick[name] })));
  const live = d.markets.filter((m) => m.closed?.w_bps != null && m.closed?.expect)
    .map((m) => ({ tk: m.ticker, label: m.label, w: m.closed.w_bps, snap: m.closed.expect.snap_bps }));
  const latest = [...all].sort((a, b) => b.close_ms - a.close_ms).slice(0, 8);
  const f = p.fade;
  return (
    <section id="p-WKND" className="panel">
      <Head code="WKND" title="Weekend record"
        meta={[["Sample", `${p.n} weekends`], ["Data", "hourly closes"], ["Fade t", f.t ?? "-"]]}
        pills={[<Pill key="c" kind={f.t == null ? "mute" : Math.abs(f.t) >= 2 ? "good" : Math.abs(f.t) >= 1 ? "state" : "mute"}
          title="How sure the fade result is: t under 1 is noise, 1 to 2 a hint, 2 or more significant">
          {f.t == null ? "No result" : Math.abs(f.t) >= 2 ? "Significant" : Math.abs(f.t) >= 1 ? "Hint" : "Noise"}</Pill>]} />
      <div className="term-pad">
        {!p.n ? (
          <p className="plan-note muted">{d.enabled ? "Reading Hyperliquid's hourly history…" : "The record is read in live mode."}</p>
        ) : (
          <>
            <p className="term-say">
              {p.n} weekends. The move an hour after the reopen against the weekend's own move:
              {p.fit && <> <b>{(p.fit.b * 100).toFixed(0)}%</b> of it kept, {bps(p.fit.a)} on top{p.fit.r2 != null && <span className="muted"> (explains {(p.fit.r2 * 100).toFixed(0)}%)</span>}.</>}
            </p>
            <p className="term-say">
              Fading every weekend move of {w.fade_min_bps}+ bps, in just before the reopen and out an hour after,
              {f.n ? <> <b className={tone(f.avg_bps)}>{bps(f.avg_bps)}</b> a trade after a {w.cost_bps} bps round trip over {f.n}, {(f.win * 100).toFixed(0)}% won{f.t != null && <span className="muted"> (t {f.t})</span>}.</> : " none that big yet."}
            </p>
            <div className="term-split">
              <Scatter rows={all} fit={p.fit} live={live} />
              <table className="plan-table">
                <thead><tr><th>Market</th><th>Weekends</th><th title="Share of the weekend move still there an hour after the reopen">Kept</th><th>Fades</th><th>Each</th></tr></thead>
                <tbody>
                  {Object.entries(w.markets).map(([name, x]) => (
                    <tr key={name}>
                      <td>{label[name] || name}</td>
                      <td>{x.n}</td>
                      <td>{x.fit ? `${(x.fit.b * 100).toFixed(0)}%` : "-"}</td>
                      <td>{x.fade.n}</td>
                      <td className={tone(x.fade.avg_bps)}>{x.fade.n ? bps(x.fade.avg_bps) : "-"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <h3 className="term-h3">Latest weekends</h3>
            <table className="plan-table">
              <thead><tr><th>Friday</th><th>Market</th>
                <th title="Hyperliquid's price just before the reopen, against Friday's close">Weekend</th>
                <th title="The price an hour after the reopen, against Friday's close">Reopen</th>
                <th title="What happened after the reopen: the reopen move minus the weekend move. The same sign as the weekend means the real market went further; the opposite sign means it gave some back">After the reopen</th></tr></thead>
              <tbody>
                {latest.map((r) => (
                  <tr key={`${r.market}-${r.close_ms}`}>
                    <td>{dayLabel(r.close_ms)}</td>
                    <td>{r.market}</td>
                    <td className={tone(r.w_bps)}>{bps(r.w_bps)}</td>
                    <td className={tone(r.r_bps)}>{bps(r.r_bps)}</td>
                    <td className={tone(r.snap_bps)}>
                      {bps(r.snap_bps)}{" "}
                      <span className="muted">{Math.abs(r.snap_bps) < 5 ? "held" : (r.snap_bps > 0) === (r.w_bps > 0) ? "went further" : "gave back"}</span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        )}
        <p className="plan-note muted">
          Published study: {w.published.source}, reopen = {w.published.b} × weekend {w.published.a < 0 ? "−" : "+"} {Math.abs(w.published.a)} bps.
          Each market uses that until it has 8 weekends of its own.
        </p>
      </div>
    </section>
  );
}

function Roll({ d, now }) {
  const futures = d.status?.["Futures prices"];
  const ff = fresh(d, "Futures prices", now, 30 * 60_000);
  const rolling = d.roll.some((r) => r.phase === "during");
  return (
    <section id="p-ROLL" className="panel">
      <Head code="ROLL" title="Oil roll"
        meta={[["Window", "business days 5 to 10"], ["Futures", ff.state === "down" ? "unreachable" : `Yahoo, ${ff.age}`]]}
        pills={[<Pill key="p" kind={rolling ? "state" : "mute"}>{rolling ? "Rolling" : "Upcoming"}</Pill>,
          <Pill key="f" kind={ff.state === "down" ? "bad" : "mute"} title="Yahoo's futures prices run about 10 minutes late">Delayed</Pill>]} />
      <div className="term-pad term-stack">
        {d.roll.map((r) => {
          const today = new Date(d.now_ms).toLocaleDateString("en-CA", { timeZone: "America/New_York" });
          return (
            <div key={r.name} className="term-roll">
              <div className="term-roll-top">
                <b>{r.label}</b>
                <span className="muted">{r.front} → {r.next}</span>
                <span className={`bm-status ${r.phase === "during" ? "live" : ""}`}>{r.phase === "during" ? "Rolling now" : r.phase === "before" ? "Coming" : "Done"}</span>
              </div>
              <div className="term-days">
                {r.days.slice(4, 10).map((x) => (
                  <span key={x.date} className={`term-day ${x.date === today ? "on" : ""}`} title={`${Math.round(x.front_weight * 100)}% ${r.front}, ${Math.round((1 - x.front_weight) * 100)}% ${r.next}`}>
                    {isoDay(x.date)}
                    <i style={{ width: `${(1 - x.front_weight) * 100}%` }} />
                  </span>
                ))}
              </div>
              {r.drift ? (
                <p className="term-say">
                  {r.front} {px(r.front_px)} · {r.next} {px(r.next_px)}: the next contract is {Math.abs(r.drift.gap_pct)}% {r.drift.gap_pct < 0 ? "cheaper" : "dearer"},
                  so the oracle has <b className={tone(r.drift.left_bps)}>{bps(r.drift.left_bps)}</b> still to move from the roll alone.
                </p>
              ) : (
                <p className="plan-note muted">Futures prices {futures && !futures.ok ? "can't be reached right now" : "not read yet"}, so the size of the drift isn't shown.</p>
              )}
              <p className="plan-note muted">
                On Hyperliquid: price vs oracle {bps(r.basis_bps, 1)} · funding {r.funding_apr == null ? "-" : fmtPct(r.funding_apr, 1)} a year.
                A price below the oracle ahead of a downward roll means traders see it coming.
              </p>
            </div>
          );
        })}
        <p className="plan-note muted">trade.xyz moves the oracle 20% a day from the front contract to the next. Futures prices are Yahoo's, about 10 minutes late.</p>
      </div>
    </section>
  );
}

function Currencies({ d, now }) {
  const f = fresh(d, "Hyperliquid xyz", now, 60_000);
  return (
    <section id="p-FX" className="panel">
      <Head code="FX" title="Currencies" meta={[["Venue", "trade.xyz"], ["Updated", f.age]]}
        pills={[<Pill key="y" kind={d.yen_watch ? "bad" : "mute"} title="A fast yen rally: 0.5% in an hour or 1% in a day">{d.yen_watch ? "Yen alert" : "Yen calm"}</Pill>,
          <SourcePill key="s" f={f} />]} />
      <div className="term-pad">
        {d.yen_watch && <div className="term-watch">{d.yen_watch}</div>}
        {!d.fx.length ? (
          <p className="plan-note muted">No currency markets set (ANALYTIX_EDGE_COINS).</p>
        ) : (
          <table className="plan-table">
            <thead><tr><th>Pair</th><th>Price</th><th>1h</th><th>24h</th><th>48h</th><th /></tr></thead>
            <tbody>
              {d.fx.map((m) => {
                const yen = m.ticker === "JPY";
                const c = m.change_24h_pct;
                const dollarUp = m.ticker === "DXY" ? c > 0 : c < 0;     // EUR/USD and GBP/USD rise as the dollar falls
                return (
                  <tr key={m.name} data-mk={m.ticker}>
                    <td><b>{m.label}</b></td>
                    <td className="num"><Flash value={m.price}>{px(m.price)}</Flash></td>
                    <td className="num">{fmtPct(m.change_1h_pct)}</td>
                    <td className="num">{fmtPct(c)}</td>
                    <td><Spark v={m.spark} w={70} /></td>
                    <td className="muted">{c == null || Math.abs(c) < 0.05 ? "" : yen ? (c < 0 ? "yen stronger" : "yen weaker") : dollarUp ? "dollar stronger" : "dollar weaker"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
        <p className="plan-note muted">
          USD/JPY falling means the yen is rising. A fast yen rally made yen loans dearer in August 2024 and carry trades were
          sold everywhere at once. Watching only: not tested yet.
        </p>
      </div>
    </section>
  );
}

function Coming({ d }) {
  const byDay = [];
  for (const e of d.events) {
    const k = e.all_day ? isoDayLong(e.t_ms) : dayLabel(e.t_ms);
    if (!byDay.length || byDay.at(-1)[0] !== k) byDay.push([k, []]);
    byDay.at(-1)[1].push(e);
  }
  return (
    <section id="p-CAL" className="panel">
      <Head code="CAL" title="Coming up" meta={[["Range", "7 days"], ["Times", "yours"]]}
        pills={[<Pill key="n">{d.events.length} events</Pill>]} />
      <div className="term-pad term-events">
        {byDay.map(([day, evs]) => (
          <div key={day}>
            <div className="term-evday">{day}</div>
            {evs.map((e) => (
              <div key={`${e.t_ms}-${e.title}`} className="term-ev">
                <span className="muted num">{e.all_day ? "all day" : clock(e.t_ms)}</span>
                <span>
                  {e.title}
                  {e.note && <span className="muted"> · {e.note}</span>}
                </span>
                <span className={`term-tag ${e.group}`}>{GROUP[e.group] || e.group}</span>
              </div>
            ))}
          </div>
        ))}
      </div>
    </section>
  );
}

function Board({ d }) {
  const rec = d.recording;
  return (
    <section id="p-EDGE" className="panel term-wide">
      <Head code="EDGE" title="Edge board"
        meta={[["Recording", rec.on && rec.markets.length ? rec.markets.map((m) => m.split(":").pop()).join(" ") : "off"]]}
        pills={["testing", "watching", "planned"].map((k) => {
          const n = d.board.filter((c) => c.status === k).length;
          return n ? <Pill key={k} kind={k === "testing" ? "good" : k === "watching" ? "state" : "mute"}>{n} {STATUS[k].toLowerCase()}</Pill> : null;
        })} />
      <div className="term-board">
        {d.board.map((c) => (
          <article key={c.key} className={`term-card ${c.status}`}>
            <div className="term-card-top">
              <b>{c.name}</b>
              <span className={`term-pill ${c.status}`}>{STATUS[c.status] || c.status}</span>
            </div>
            <div className="muted term-card-mk">{c.markets}</div>
            <p>{c.idea}</p>
            {c.line && <p className="term-card-line">{c.line}</p>}
            <p className="muted">Next: {c.next}</p>
          </article>
        ))}
      </div>
    </section>
  );
}

/* What changed since the page opened, or the latest headlines with how the market moved after each. */
function Log({ log, since, coins }) {
  const [tab, setTab] = useState("changes");
  const [news, setNews] = useState(null);
  useEffect(() => {
    if (tab !== "news") return undefined;
    let live = true;
    let timer;
    const load = async () => {
      try {
        const r = await fetch("/api/news?hours=24");
        const j = await r.json();
        if (live && r.ok) setNews(j);
      } catch {
        /* tried again in a minute */
      }
      if (live) timer = setTimeout(load, 60_000);
    };
    load();
    return () => {
      live = false;
      clearTimeout(timer);
    };
  }, [tab]);
  const items = (news?.recent || []).slice(0, 30);
  const first = coins?.[0];
  return (
    <section id="p-LOG" className="panel">
      <Head code="LOG" title={tab === "changes" ? "What changed" : "Headlines"}
        meta={tab === "changes" ? [["Kept", "7 days"], ["Since", since ? new Date(since).toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" }) : "-"]] : [["Range", "24 h"]]}
        pills={[<Pill key="n">{tab === "changes" ? `${log.length} lines` : `${items.length} items`}</Pill>]}>
        <span className="tp-tabs" role="tablist">
          {[["changes", "Changes"], ["news", "Headlines"]].map(([k, t]) => (
            <button key={k} role="tab" aria-selected={tab === k} className={tab === k ? "on" : ""} onClick={() => setTab(k)}>{t}</button>
          ))}
        </span>
      </Head>
      {tab === "changes" ? <ChangeLog log={log} /> : (
        <div className="term-log lg-news">
          {!news && <div className="muted">Loading the headlines…</div>}
          {news && !items.length && <div className="muted">{news.enabled ? "No headlines in the last 24 hours." : "Headlines are read in live mode only."}</div>}
          {items.map((it) => {
            const r = it.reaction?.[first] || Object.values(it.reaction || {})[0];
            const mins = Math.max(0, Math.round((Date.now() - it.t_ms) / 60000));
            return (
              <div key={it.id} className="lg-item">
                <span className="muted num">{mins < 60 ? `${mins}m` : `${Math.round(mins / 60)}h`}</span>
                <span className="lg-title">
                  {it.url ? <a href={it.url} target="_blank" rel="noreferrer">{it.title}</a> : it.title}
                  <span className="muted"> {it.source}{it.coins?.length ? ` · ${it.coins.join(" ")}` : ""}</span>
                </span>
                <span className={`num lg-r ${r?.after_bps > 0 ? "up" : r?.after_bps < 0 ? "down" : "muted"}`}
                  title={r ? `${first || ""} after it: ${r.after_bps} bps${r.done ? " in 30 min" : " so far"}` : "no reaction measured"}>
                  {r?.after_bps == null ? "" : `${r.after_bps > 0 ? "+" : ""}${Math.round(r.after_bps)}`}
                </span>
              </div>
            );
          })}
        </div>
      )}
    </section>
  );
}

function Boot({ error }) {
  return (
    <div className="term-boot" role="status">
      <div>&gt; connecting to the Analytix server</div>
      <div>&gt; reading trade.xyz markets on Hyperliquid</div>
      <div>&gt; loading the weekend record and the calendar</div>
      <div>&gt; {error || "almost there"}<span className="cursor" aria-hidden="true" /></div>
    </div>
  );
}

export default function Edges({ d, error, now, onPick = () => {} }) {
  const { orders, info } = useOrders(Boolean(d));
  const [flowMin, setFlowMin] = useState(60);
  const flow = useFlow(flowMin, Boolean(d));             // one poll for the river, the constellation and the radar
  if (!d) return <Boot error={error} />;
  now = now || d.now_ms;
  const down = Object.entries(d.status || {}).filter(([, s]) => !s.ok);
  return (
    <div className="term-grid">
      {error && <div className="plan-error term-wide">{error}</div>}
      <div className="tw-8"><World d={d} now={now} orders={orders} info={info} /></div>
      <div className="tw-4"><Log log={d.log || []} since={d.log_since} coins={d.coins} /></div>
      <Charts d={d} onPick={onPick} />
      <Flow d={d} now={now} data={flow} minutes={flowMin} setMinutes={setFlowMin} />
      <div className="tw-7"><Constellation d={d} flow={flow} /></div>
      <div className="tw-5"><Radar flow={flow} /></div>
      <Markets d={d} now={now} />
      <div className="tw-7"><Record d={d} /></div>
      <div className="tw-5 term-col">
        <Roll d={d} now={now} />
        <Currencies d={d} now={now} />
      </div>
      <div className="tw-4"><Coming d={d} /></div>
      <div className="tw-8"><Board d={d} /></div>
      {down.length > 0 && (
        <p className="plan-note muted term-wide">Not reachable right now: {down.map(([s]) => s).join(", ")}.</p>
      )}
    </div>
  );
}

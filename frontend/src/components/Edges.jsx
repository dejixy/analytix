import { fmtPct, fmtUsd } from "../format.js";

/* The edge terminal's main view (/api/edges): trade.xyz's commodities and currencies on Hyperliquid, the weekend
   record, the oil roll, what's coming up and the board of edges being worked on. Times are in your time zone. */

const px = (p) => (p == null ? "-" : p.toLocaleString(undefined, { maximumFractionDigits: p >= 1000 ? 1 : p >= 10 ? 2 : 4, minimumFractionDigits: p >= 1000 ? 1 : p >= 10 ? 2 : 4 }));
const bps = (x, d = 0) => (x == null ? "-" : `${x > 0 ? "+" : x < 0 ? "−" : ""}${Math.abs(x).toFixed(d)} bps`);
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
const GROUP = { oil: "Oil", macro: "Macro", session: "Market hours", crypto: "Crypto" };
const STATUS = { testing: "Testing", watching: "Watching", planned: "Planned" };

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

function Markets({ d }) {
  return (
    <section className="panel term-wide">
      <div className="panel-head">
        <h2 className="panel-title">Commodities on Hyperliquid</h2>
        <span className="panel-sub">trade.xyz · fees {d.fees.taker_bps} bps taker, {d.fees.maker_bps} bps maker</span>
      </div>
      <div className="term-scroll">
        <table className="plan-table term-table">
          <thead>
            <tr>
              <th>Market</th><th>Real market</th><th>Price</th><th>24h</th>
              <th title="Hyperliquid's price against trade.xyz's oracle (the real market's price while it's open)">vs oracle</th>
              <th>Funding / yr</th><th>Open interest</th><th>24h volume</th>
            </tr>
          </thead>
          <tbody>
            {d.markets.map((m) => [
              <tr key={m.name}>
                <td><b>{m.label}</b> <span className="muted">{m.ticker}</span></td>
                <td><RealMarket m={m} now={d.now_ms} /></td>
                <td className="num">{px(m.price)}</td>
                <td className={`num ${tone(m.change_24h_pct)}`}>{fmtPct(m.change_24h_pct)}</td>
                <td className="num">{bps(m.basis_bps, 1)}</td>
                <td className="num">{m.funding_apr == null ? "-" : fmtPct(m.funding_apr, 1)}</td>
                <td className="num">{fmtUsd(m.oi_usd)}</td>
                <td className="num">{fmtUsd(m.day_volume)}</td>
              </tr>,
              m.closed && (
                <tr key={`${m.name}-closed`} className="term-closed">
                  <td colSpan={8}>
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

function Scatter({ rows, fit }) {
  const pts = rows.filter((r) => r.w_bps != null && r.r_bps != null);
  if (pts.length < 3) return null;
  const lim = Math.max(50, ...pts.map((r) => Math.max(Math.abs(r.w_bps), Math.abs(r.r_bps)))) * 1.1;
  const W = 260, H = 190, pad = 26;
  const x = (v) => pad + ((v + lim) / (2 * lim)) * (W - 2 * pad);
  const y = (v) => H - pad - ((v + lim) / (2 * lim)) * (H - 2 * pad);
  return (
    <svg className="term-scatter" viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Weekend move against the move at the reopen">
      <line x1={x(-lim)} y1={y(0)} x2={x(lim)} y2={y(0)} className="term-axis" />
      <line x1={x(0)} y1={y(-lim)} x2={x(0)} y2={y(lim)} className="term-axis" />
      <line x1={x(-lim)} y1={y(-lim)} x2={x(lim)} y2={y(lim)} className="term-same" />
      {fit && <line x1={x(-lim)} y1={y(fit.a - fit.b * lim)} x2={x(lim)} y2={y(fit.a + fit.b * lim)} className="term-fit" />}
      {pts.map((r) => (
        <circle key={`${r.market}-${r.close_ms}`} cx={x(r.w_bps)} cy={y(r.r_bps)} r="2.6" className="term-pt">
          <title>{`${r.market} · ${dayLabel(r.close_ms)}: weekend ${bps(r.w_bps)}, reopen ${bps(r.r_bps)}`}</title>
        </circle>
      ))}
      <text x={W - pad} y={y(0) - 4} textAnchor="end" className="term-ax-label">weekend move</text>
      <text x={x(0) + 4} y={pad - 8} className="term-ax-label">at the reopen</text>
    </svg>
  );
}

function Record({ d }) {
  const w = d.weekend;
  const p = w.pooled;
  const label = Object.fromEntries(d.markets.map((m) => [m.name, m.label]));
  const all = Object.entries(w.markets).flatMap(([name, x]) => x.rows.map((r) => ({ ...r, market: label[name] || name })));
  const latest = [...all].sort((a, b) => b.close_ms - a.close_ms).slice(0, 8);
  const f = p.fade;
  return (
    <section className="panel">
      <div className="panel-head">
        <h2 className="panel-title">Weekend record</h2>
        <span className="panel-sub">hourly closes · holiday weekends left out</span>
      </div>
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
              <Scatter rows={all} fit={p.fit} />
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
              <thead><tr><th>Friday</th><th>Market</th><th>Weekend</th><th>Reopen</th><th>Gave back</th></tr></thead>
              <tbody>
                {latest.map((r) => (
                  <tr key={`${r.market}-${r.close_ms}`}>
                    <td>{dayLabel(r.close_ms)}</td>
                    <td>{r.market}</td>
                    <td className={tone(r.w_bps)}>{bps(r.w_bps)}</td>
                    <td className={tone(r.r_bps)}>{bps(r.r_bps)}</td>
                    <td className={tone(r.snap_bps)}>{bps(r.snap_bps)}</td>
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

function Roll({ d }) {
  const futures = d.status?.["Futures prices"];
  return (
    <section className="panel">
      <div className="panel-head">
        <h2 className="panel-title">Oil roll</h2>
        <span className="panel-sub">5th to 10th business day</span>
      </div>
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

function Currencies({ d }) {
  return (
    <section className="panel">
      <div className="panel-head">
        <h2 className="panel-title">Currencies</h2>
        <span className="panel-sub">trade.xyz on Hyperliquid</span>
      </div>
      <div className="term-pad">
        {d.yen_watch && <div className="term-watch">{d.yen_watch}</div>}
        {!d.fx.length ? (
          <p className="plan-note muted">No currency markets set (ANALYTIX_EDGE_COINS).</p>
        ) : (
          <table className="plan-table">
            <thead><tr><th>Pair</th><th>Price</th><th>1h</th><th>24h</th><th /></tr></thead>
            <tbody>
              {d.fx.map((m) => {
                const yen = m.ticker === "JPY";
                const c = m.change_24h_pct;
                const dollarUp = m.ticker === "DXY" ? c > 0 : c < 0;     // EUR/USD and GBP/USD rise as the dollar falls
                return (
                  <tr key={m.name}>
                    <td><b>{m.label}</b></td>
                    <td className="num">{px(m.price)}</td>
                    <td className="num">{fmtPct(m.change_1h_pct)}</td>
                    <td className="num">{fmtPct(c)}</td>
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
    <section className="panel">
      <div className="panel-head">
        <h2 className="panel-title">Coming up</h2>
        <span className="panel-sub">7 days · your time</span>
      </div>
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
    <section className="panel term-wide">
      <div className="panel-head">
        <h2 className="panel-title">Edge board</h2>
        <span className="panel-sub">
          {rec.on && rec.markets.length ? `recording ${rec.markets.map((m) => m.split(":").pop()).join(", ")}` : "edge markets not recording yet"}
        </span>
      </div>
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

export default function Edges({ d, error }) {
  if (!d) return <p className="plan-note muted term-pad">{error || "Loading…"}</p>;
  const down = Object.entries(d.status || {}).filter(([, s]) => !s.ok);
  return (
    <div className="term-grid">
      {error && <div className="plan-error term-wide">{error}</div>}
      <Markets d={d} />
      <div className="term-col">
        <Record d={d} />
        <Currencies d={d} />
      </div>
      <div className="term-col">
        <Roll d={d} />
        <Coming d={d} />
      </div>
      <Board d={d} />
      {down.length > 0 && (
        <p className="plan-note muted term-wide">Not reachable right now: {down.map(([s]) => s).join(", ")}.</p>
      )}
    </div>
  );
}

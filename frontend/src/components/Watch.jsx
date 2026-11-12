import { Head, Pill } from "./TermPanel.jsx";

/* Watchers (api/watchers.py): fixed rules that open trade tickets to place by hand, each one followed as a paper
   trade so every watcher builds a record on days it has never seen. Open tickets first, then what each watcher is
   reading right now (why it hasn't fired), then the last closed tickets. */

const bps = (x) => {
  if (x == null) return "-";
  const r = Math.round(x);
  return `${r > 0 ? "+" : r < 0 ? "−" : ""}${Math.abs(r)} bps`;
};
const pct = (x) => (x == null ? "-" : `${x >= 0 ? "" : "−"}${Math.abs(x).toFixed(0)}%`);
const tone = (x) => (x == null || Math.round(x) === 0 ? "" : x > 0 ? "up" : "down");
const px = (p) => (p == null ? "-" : p.toLocaleString(undefined, { maximumFractionDigits: p >= 1000 ? 0 : p >= 10 ? 2 : 4 }));
const when = (ms) => {
  const x = new Date(ms);
  return `${x.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" })}, ${x.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hourCycle: "h23" })}`;
};
const ago = (ms) => {
  const m = Math.max(0, Math.round(ms / 60000));
  return m < 60 ? `${m}m` : m < 48 * 60 ? `${Math.floor(m / 60)}h ${m % 60}m` : `${Math.floor(m / 1440)}d`;
};
const SIDE = { long: "Long", short: "Short", carry: "Carry" };
const status = (r) => (r.ticket_open ? "Ticket open" : r.why_not || "Fires on the next check");
const short = (c) => (c || "").replace("xyz:", "").replace("CL", "WTI").replace("BRENTOIL", "Brent");

function Ticket({ t, now }) {
  const p = t.pnl;
  return (
    <article className={`wt-ticket ${t.side}`}>
      <div className="wt-ticket-top">
        <span className={`wt-side ${t.side}`}>{SIDE[t.side]}</span>
        <b>{t.title}</b>
        <span className="wt-now">
          <span className="muted">now</span> <b className={`num ${tone(p.net_bps)}`}>{bps(p.net_bps)}</b>
          <span className="muted"> after costs</span>
        </span>
      </div>
      <div className="wt-legs num">
        {t.legs.map((g) => (
          <span key={g.kind + g.coin}>
            {g.dir > 0 ? "Buy" : "Short"} {short(g.coin)}{g.kind === "spot" ? " spot" : ""} at {px(g.entry)} · now {px(g.last)}
          </span>
        ))}
        <span className="muted">price {bps(p.price_bps)} · funding {bps(p.funding_bps)} · fees {bps(-p.cost_bps)}</span>
      </div>
      <ul className="wt-why">{t.why.map((w) => <li key={w}>{w}</li>)}</ul>
      <p className="plan-note muted">
        {t.exit} Opened {ago(now - t.opened_ms)} ago, expected {bps(t.expect_bps)}.
        {t.gaps ? ` ${t.gaps} outage${t.gaps > 1 ? "s" : ""}: funding not counted while the server was down.` : ""}
      </p>
    </article>
  );
}

function RollReads({ reads, now }) {
  return (
    <table className="wt-reads">
      <thead><tr><th>Market</th><th>On front</th><th>Roll left</th><th>vs oracle</th><th>Not priced</th><th>Expects</th><th>Status</th></tr></thead>
      <tbody>
        {reads.map((r) => (
          <tr key={r.market}>
            <td>{r.label} <span className="muted">{r.front}→{r.next}</span></td>
            <td className="num">{Math.round(r.front_weight * 100)}%</td>
            <td className={`num ${tone(r.left_bps)}`}>{bps(r.left_bps)}</td>
            <td className={`num ${tone(r.basis_bps)}`}>{bps(r.basis_bps)}</td>
            <td className={`num ${tone(r.unpriced_bps)}`}>{bps(r.unpriced_bps)}</td>
            <td className={`num ${tone(r.expect_bps)}`}>{bps(r.expect_bps)}</td>
            <td className={r.ticket_open ? "" : "muted"}>{status(r)}{r.next_step_ms && r.phase !== "after" ? ` · next step ${when(r.next_step_ms)}` : ""}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function CarryReads({ reads }) {
  return (
    <table className="wt-reads">
      <thead><tr><th>Coin</th><th>Funding now</th><th>Last day</th><th>3 days</th><th>Expects</th><th>Status</th></tr></thead>
      <tbody>
        {reads.map((r) => (
          <tr key={r.market}>
            <td>{r.market} <span className="muted">/ {r.spot}</span></td>
            <td className={`num ${tone(r.now_apr)}`}>{pct(r.now_apr)}</td>
            <td className={`num ${tone(r.day_apr)}`}>{pct(r.day_apr)}</td>
            <td className={`num ${tone(r.three_day_apr)}`}>{pct(r.three_day_apr)}</td>
            <td className={`num ${tone(r.expect_bps)}`}>{bps(r.expect_bps)}</td>
            <td className={r.ticket_open ? "" : "muted"}>{status(r)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function Record({ r }) {
  if (!r.n) return <span className="muted">No closed tickets yet{r.open ? `, ${r.open} open` : ""}.</span>;
  return (
    <span>
      {r.n} closed, {r.won} made money · average <b className={`num ${tone(r.avg_bps)}`}>{bps(r.avg_bps)}</b> after costs
      <span className="muted"> (they expected {bps(r.expected_avg_bps)}) · total {bps(r.total_bps)}{r.open ? ` · ${r.open} open` : ""}</span>
    </span>
  );
}

export default function Watch({ w, now }) {
  if (!w) {
    return (
      <section id="p-WATCH" className="panel term-wide">
        <Head code="WATCH" title="Trade watchers" />
        <p className="term-pad plan-note muted">The watchers run on the server in live mode only.</p>
      </section>
    );
  }
  const open = w.open || [];
  const errs = Object.keys(w.errors || {});
  return (
    <section id="p-WATCH" className="panel term-wide">
      <Head code="WATCH" title="Trade watchers"
        meta={[["Checked", w.updated_ms ? `${ago(now - w.updated_ms)} ago` : "starting"], ["Rules", "fixed, paper-tracked"]]}
        pills={[<Pill key="o" kind={open.length ? "live" : "mute"}>{open.length ? `${open.length} open` : "No tickets"}</Pill>,
          errs.length ? <Pill key="e" kind="bad" title={errs.map((k) => `${k}: ${w.errors[k]}`).join("\n")}>{errs.length} source{errs.length > 1 ? "s" : ""} down</Pill> : null]} />
      <div className="term-pad wt-body">
        {open.length > 0 && <div className="wt-tickets">{open.map((t) => <Ticket key={t.id} t={t} now={now} />)}</div>}
        <div className="wt-watchers">
          {w.watchers.map((x) => (
            <div key={x.key} className="wt-watcher">
              <div className="wt-watcher-top"><b>{x.name}</b><Record r={x.record} /></div>
              <p className="plan-note muted">{x.rule}</p>
              {x.reads.length ? (x.key === "roll" ? <RollReads reads={x.reads} now={now} /> : <CarryReads reads={x.reads} />)
                : <p className="plan-note muted">Nothing read yet.</p>}
            </div>
          ))}
        </div>
        {w.closed?.length > 0 && (
          <div className="wt-closed">
            <div className="muted wt-closed-head">Last closed</div>
            {w.closed.map((t) => (
              <div key={t.id} className="wt-closed-line">
                <span className="muted num">{when(t.closed_ms)}</span>
                <span>{t.title.split(":")[0]}</span>
                <b className={`num ${tone(t.result?.net_bps)}`}>{bps(t.result?.net_bps)}</b>
                <span className="muted">expected {bps(t.expect_bps)} · {t.closed_why}</span>
              </div>
            ))}
          </div>
        )}
        <p className="plan-note muted">
          A ticket is a trade to place yourself on Hyperliquid; nothing is traded for you. Every ticket is followed here with
          taker fees, so each watcher's record shows whether its rule actually pays. Alerts go to Telegram when it's set up
          (alert settings → Watchers).
        </p>
      </div>
    </section>
  );
}

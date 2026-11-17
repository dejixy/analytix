import { Fragment, useEffect, useState } from "react";
import { fmtTime } from "./format.js";

/* The bets suite (/bets, /api/bets): Nigerian bookmakers' football odds against a sharp price, history and form.
   Today's rollover pick and its paper chain, the flat value bets, the paper record, what history says, and the
   sources. Every bet here is a paper bet until you place it yourself. Colours: green and red for data only. */

const EVERY_MS = 30_000;
const naira = (v) => (v == null ? "-" : `${v < 0 ? "−" : ""}₦${Math.abs(v).toLocaleString(undefined, { maximumFractionDigits: 0 })}`);
const pct = (v, d = 0) => (v == null ? "-" : `${(v * 100).toFixed(d)}%`);
const sgn = (v, d = 1) => (v == null ? "-" : `${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v * 100).toFixed(d)}%`);
const tone = (v) => (v == null || Math.abs(v) < 1e-9 ? "" : v > 0 ? "up" : "down");
const odd = (v) => (v == null ? "-" : Number(v).toFixed(2));
const kick = (ms) => new Date(ms).toLocaleString([], { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
const ago = (ms) => {
  if (ms == null) return "-";
  const m = Math.max(0, Math.round(ms / 60000));
  return m < 1 ? "now" : m < 60 ? `${m}m ago` : m < 2880 ? `${Math.floor(m / 60)}h ${m % 60}m ago` : `${Math.floor(m / 1440)}d ago`;
};
const MARKET = { 1: "Home win", X: "Draw", 2: "Away win", "1X": "Home or draw", 12: "Either team wins", X2: "Away or draw" };
const pick = (x) => (x.market === "1" ? x.home : x.market === "2" ? x.away : x.market === "1X" ? `${x.home} or draw`
  : x.market === "X2" ? `${x.away} or draw` : x.market === "X" ? "Draw" : `No draw: ${x.home} or ${x.away}`);
const BOOK = { bet9ja: "Bet9ja", betking: "BetKing", nairabet: "Nairabet", sportybet: "SportyBet" };

const Top = ({ title, facts = [], children }) => (
  <div className="az-top">
    <h2>{title}</h2>
    {children}
    <span className="az-facts">{facts.filter(Boolean).map((f, i) => <span key={i}>{f}</span>)}</span>
  </div>
);
const Seg = ({ value, set, opts }) => (
  <span className="az-seg" role="group">
    {opts.map(([k, t]) => <button key={k} className={value === k ? "on" : ""} aria-pressed={value === k} onClick={() => set(k)}>{t}</button>)}
  </span>
);

function Why({ x }) {
  const f = x.fav_form, o = x.opp_form, pat = x.pattern, rec = x.fav_record;
  return (
    <ul className="bt-why">
      <li>
        Sharp price says <b>{pct(x.fair_p, 1)}</b> ({x.sharp_src}); history moves it to <b>{pct(x.p, 1)}</b>.
        At {odd(x.odds)} on {BOOK[x.book] || x.book} that is an edge of <b className={tone(x.edge)}>{sgn(x.edge)}</b>.
      </li>
      {x.agree_why && <li>Form: <span className={x.agree ? "up" : x.agree === false ? "down" : ""}>{x.agree ? "backs it" : x.agree === false ? "doesn't back it" : "unclear"}</span>, {x.agree_why}.</li>}
      {f && o && <li>Last {f.n}: {x.fav} {f.results} ({f.gf.toFixed(1)} scored, {f.ga.toFixed(1)} conceded a game) · opponent {o.results} ({o.gf.toFixed(1)}, {o.ga.toFixed(1)}).</li>}
      {pat && <li>History: favourites priced {pat.range[0]}–{pat.range[1]} in {pat.scope} won {pct(pat.rate, 1)} of {pat.n.toLocaleString()} games, against {pct(pat.implied, 1)} the prices said.</li>}
      {rec && <li>{x.fav} as a clear favourite over three seasons: won {rec.won} of {rec.n} ({pct(rec.won / rec.n)}).</li>}
      {x.others?.length > 1 && <li>All books: {x.others.map((b) => `${BOOK[b.book] || b.book} ${odd(b.odds)}`).join(" · ")}.</li>}
    </ul>
  );
}

function Rollover({ r }) {
  const c = r.chain;
  const x = r.today || r.candidate;
  const used = c.refills >= r.refills_max + 1;
  return (
    <section className="panel">
      <Top title="Today's rollover pick" facts={[`chain ${c.n}`, `day ${c.day} of ${r.target}`, `refills ${c.refills} of ${r.refills_max}`]} />
      {x ? (
        <div className="bt-pick">
          <div className="bt-pick-top">
            <div>
              <div className="bt-pick-name">{pick(x)}</div>
              <div className="muted">{x.home} v {x.away} · {x.league_name || x.league} · {kick(x.t_ms)}</div>
            </div>
            <div className="bt-pick-odds">
              <b className="num">{odd(x.odds)}</b>
              <span className="muted">{BOOK[x.book] || x.book}</span>
            </div>
          </div>
          <div className="az-kpis">
            <div><span className="muted">Chance of winning</span><b className="num">{pct(x.p, 1)}</b></div>
            <div><span className="muted">Edge at this price</span><b className={`num ${tone(x.edge)}`}>{sgn(x.edge)}</b></div>
            <div><span className="muted">Stake (whole balance)</span><b className="num">{naira(r.today ? r.today.stake : c.balance)}</b></div>
            <div><span className="muted">Returns if it wins</span><b className="num up">{naira((r.today ? r.today.stake : c.balance) * x.odds)}</b></div>
          </div>
          <Why x={x} />
          <p className="az-note">
            {r.today ? (r.today.status === "open" ? "Booked for today on paper. It settles after the game; tomorrow's pick waits for it."
              : `Settled: ${r.today.status === "won" ? "won" : "lost"} ${r.today.score || ""}.`)
              : "Best candidate right now; today's pick is booked on the next pass (every 10 minutes, from 06:00 Lagos) and then fixed."}
          </p>
        </div>
      ) : <p className="az-note">Nothing qualifies right now: odds 1.15–1.35, kick-off within 30 hours, form backing the favourite, priced no worse than 2% under fair, with fresh odds from your laptop.</p>}
      <div className="bt-chain">
        <div className="az-kpis">
          <div><span className="muted">Balance</span><b className="num">{naira(c.balance)}</b></div>
          <div><span className="muted">Best run</span><b className="num">{c.best} wins</b></div>
          <div><span className="muted">Spent so far</span><b className="num">{naira(r.start * (c.refills + 1))}</b></div>
          <div><span className="muted">Chance of day {r.target} from here</span><b className="num">{r.p_finish == null ? "-" : r.p_finish < 0.0001 ? "under 0.01%" : pct(r.p_finish, 2)}</b></div>
        </div>
        <p className="az-note">
          The chance of reaching day {r.target} is today's chance raised to the {r.days_left} wins still needed, assuming every
          pick is as likely as today's. {used ? "The six refills are used up: no new chain is started." : ""}
        </p>
        {r.history.length > 0 && (
          <div className="az-scroll az-mid"><table className="az-table">
            <thead><tr><th>Day</th><th>Chain · leg</th><th>Pick</th><th className="r">Odds</th><th className="r">Stake</th><th>Result</th></tr></thead>
            <tbody>
              {r.history.map((p) => (
                <tr key={p.day}>
                  <td className="muted num">{p.day}</td><td className="num">{p.chain} · {p.leg}</td><td className="bt-name">{pick(p)}</td>
                  <td className="r num">{odd(p.odds)}</td><td className="r num">{naira(p.stake)}</td>
                  <td className={p.status === "won" ? "up" : p.status === "lost" ? "down" : "muted"}>{p.status === "open" ? "open" : `${p.status} ${p.score || ""}`}</td>
                </tr>
              ))}
            </tbody>
          </table></div>
        )}
      </div>
    </section>
  );
}

function Value({ d }) {
  const [view, setView] = useState("value");
  const [open, setOpen] = useState(null);
  const rows = view === "value" ? d.value : d.near;
  const [lo, hi, edge, stake] = d.settings.flat;
  return (
    <section className="panel">
      <Top title="Value bets, flat stake" facts={[`${d.value.length} now`, `odds ${lo}–${hi}`, `edge ${pct(edge)}+`, `${naira(stake)} a bet`]}>
        <Seg value={view} set={setView} opts={[["value", "Value"], ["near", `Everything ${lo}–${hi}`]]} />
      </Top>
      {rows.length ? (
        <div className="az-scroll az-tall"><table className="az-table az-click">
          <thead><tr><th>Kick-off</th><th>Bet</th><th className="r">Best odds</th><th className="r">Fair odds</th><th className="r">Edge</th><th className="r">Chance</th><th>Form</th><th className="r">History</th></tr></thead>
          <tbody>
            {rows.map((x) => (
              <Fragment key={x.key}>
                <tr onClick={() => setOpen(open === x.key ? null : x.key)} className={open === x.key ? "on" : ""}>
                  <td className="muted num">{kick(x.t_ms)}</td>
                  <td className="bt-name"><b>{pick(x)}</b><span className="muted az-cat">{x.home} v {x.away} · {x.league_name || x.league}</span></td>
                  <td className="r num">{odd(x.odds)} <span className="muted">{BOOK[x.book] || x.book}</span></td>
                  <td className="r num muted">{odd(1 / x.p)}</td>
                  <td className={`r num ${tone(x.edge)}`}>{sgn(x.edge)}</td>
                  <td className="r num">{pct(x.p, 1)}</td>
                  <td className={x.agree ? "up" : x.agree === false ? "down" : "muted"} title={x.agree_why || ""}>{x.agree ? "backs it" : x.agree === false ? "against" : "-"}</td>
                  <td className="r num muted">{x.pattern ? `${pct(x.pattern.rate)} of ${x.pattern.n.toLocaleString()}` : "-"}</td>
                </tr>
                {open === x.key && <tr className="bt-detail"><td colSpan={8}><Why x={x} /></td></tr>}
              </Fragment>
            ))}
          </tbody>
        </table></div>
      ) : (
        <p className="az-note">
          {d.board_n ? `None of the ${d.board_n} prices matched to a sharp line is above fair by ${pct(edge)} or more right now. "Everything" shows the closest.`
            : "No Nigerian odds matched yet. Run the collector on your laptop (see Sources), and the board fills within a minute."}
        </p>
      )}
      <p className="az-note">
        Edge is the odds times the history-adjusted chance, minus one: what each ₦1 staked returns on average if the sharp price is right.
        Fair odds are one over that chance. Click a row for the reasoning. Booked on paper at {naira(stake)} each the first time it appears.
      </p>
    </section>
  );
}

function Record({ rec, recent }) {
  const f = rec.flat, r = rec.rollover;
  return (
    <section className="panel term-wide">
      <Top title="Paper record" facts={[`${f.open} flat bets open`, "settled from results; closing value from the closing sharp price"]} />
      <div className="az-kpis">
        <div><span className="muted">Flat bets settled</span><b className="num">{f.n}</b></div>
        <div><span className="muted">Won</span><b className="num">{f.n ? `${f.won} (${pct(f.won / f.n)})` : "-"}</b></div>
        <div><span className="muted">Profit</span><b className={`num ${tone(f.profit)}`}>{naira(f.profit)}</b></div>
        <div><span className="muted">Return on stakes</span><b className={`num ${tone(f.roi)}`}>{f.roi == null ? "-" : sgn(f.roi)}</b></div>
        <div><span className="muted" title="The bet's odds against the fair closing price: positive means it beat the close">Beat the close by</span><b className={`num ${tone(f.clv)}`}>{f.clv == null ? "-" : `${sgn(f.clv)} (${f.clv_n})`}</b></div>
        <div><span className="muted">Rollover chains</span><b className="num">{r.n}</b></div>
        <div><span className="muted">Rollover picks won</span><b className="num">{r.picks ? `${r.won} of ${r.picks}` : "-"}</b></div>
      </div>
      {recent.length > 0 && (
        <div className="az-scroll az-mid"><table className="az-table">
          <thead><tr><th>Kick-off</th><th>Bet</th><th className="r">Odds</th><th className="r">Edge then</th><th className="r">Beat the close</th><th>Result</th><th className="r">Profit</th></tr></thead>
          <tbody>
            {recent.map((b) => (
              <tr key={b.key}>
                <td className="muted num">{kick(b.t_ms)}</td><td className="bt-name"><b>{pick(b)}</b> <span className="muted">{BOOK[b.book] || b.book}</span></td>
                <td className="r num">{odd(b.odds)}</td><td className={`r num ${tone(b.edge)}`}>{sgn(b.edge)}</td>
                <td className={`r num ${tone(b.clv)}`}>{b.clv == null ? "-" : sgn(b.clv)}</td>
                <td className={b.status === "won" ? "up" : b.status === "lost" ? "down" : "muted"}>{b.status}{b.score ? ` ${b.score}` : ""}</td>
                <td className={`r num ${tone(b.profit)}`}>{b.profit == null ? "-" : naira(b.profit)}</td>
              </tr>
            ))}
          </tbody>
        </table></div>
      )}
      <p className="az-note">
        Judge it by "beat the close" first: after 50 or more bets, consistently getting better odds than the sharp closing price is the
        clearest sign the edge is real. Profit over a few dozen short-priced bets is mostly luck.
      </p>
    </section>
  );
}

function Patterns({ rows: all }) {
  const kinds = [...new Set(all.map((r) => r.kind))];
  const [kind, setKind] = useState(null);
  const k = kind && kinds.includes(kind) ? kind : kinds[0];
  const rows = all.filter((r) => r.kind === k);
  return (
    <section className="panel">
      <Top title="What history says" facts={["three seasons, 12 leagues"]}>
        {kinds.length > 1 && <Seg value={k} set={setKind} opts={[["sharp", "Pinnacle's prices"], ["avg", "Market average"]].filter(([x]) => kinds.includes(x))} />}
      </Top>
      {rows.length ? (
        <div className="az-scroll az-mid"><table className="az-table">
          <thead><tr><th>Bet</th><th>Priced</th><th className="r">Games</th><th className="r">Won</th><th className="r">Price said</th><th className="r">Difference</th></tr></thead>
          <tbody>
            {rows.map((r) => (
              <tr key={`${r.market}${r.odds[0]}`}>
                <td>{r.name}</td><td className="num">{r.odds[0].toFixed(2)}–{r.odds[1].toFixed(2)}</td><td className="r num muted">{r.n.toLocaleString()}</td>
                <td className="r num">{pct(r.rate, 1)}</td><td className="r num muted">{pct(r.implied, 1)}</td>
                <td className={`r num ${tone(r.rate - r.implied)}`}>{sgn(r.rate - r.implied)}</td>
              </tr>
            ))}
          </tbody>
        </table></div>
      ) : <p className="az-note">The history is read once a day; the first read takes a minute or two after the server starts.</p>}
      <p className="az-note">
        A positive difference means bets at that price won more often than the price said. Each pick's chance is moved by its
        league's difference, shrunk so a small sample can't move it much, and only by history measured on the same kind of price:
        the market average carries a bigger margin, and taking it out understates favourites, so it would overstate every edge.
      </p>
    </section>
  );
}

function Sources({ s, now }) {
  const books = Object.entries(s.books);
  const st = s.status;
  const line = (k, label, extra) => {
    const x = st[k] || {};
    return (
      <div key={k}>
        <i className={`az-dot ${x.ok ? "up" : x.ok === false && !x.setup ? "down" : ""}`} />
        <b>{label}</b>
        <span className="muted" title={x.error || ""}>{x.ok ? `${ago(now - x.at)}${extra ? ` · ${extra}` : ""}` : x.ok === false ? (x.setup ? `not set up: ${x.error}` : `down: ${x.error}`) : "first read pending"}</span>
      </div>
    );
  };
  return (
    <section className="panel">
      <Top title="Sources" />
      <div className="az-src">
        {line("history", "History (football-data)", `${s.history_games.toLocaleString()} games`)}
        {line("fixtures", "Coming games (football-data)", `${s.sharp_fd} games`)}
        {line("sharp", "Sharp prices (The Odds API)", `${s.sharp_oa} games${s.credits != null ? ` · ${s.credits} credits left` : ""}`)}
        {books.length ? books.map(([k, v]) => (
          <div key={k}>
            <i className={`az-dot ${now - v.t_ms < 2 * 3600_000 ? "up" : "down"}`} />
            <b>{BOOK[k] || k}</b>
            <span className="muted">{v.games} games · sent {ago(now - v.t_ms)}{now - v.t_ms >= 2 * 3600_000 ? " (too old to bet on)" : ""}</span>
          </div>
        )) : (
          <div><i className="az-dot" /><b>Nigerian books</b><span className="muted">nothing received yet</span></div>
        )}
      </div>
      <p className="az-note">
        Nigerian odds come from your laptop, because Bet9ja and BetKing only answer Nigerian addresses. With the tunnel open
        (<code>ssh -N analytix</code>), run <code>python scripts/naijaOdds.py --loop</code> from your Analytix folder; it sends fresh odds every 20 minutes.
      </p>
    </section>
  );
}

export default function Bets() {
  const [d, setD] = useState(null);
  const [error, setError] = useState(null);
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    document.title = "Analytix · Bets";
    let live = true;
    let timer;
    const load = async () => {
      try {
        const r = await fetch("/api/bets");
        const j = await r.json();
        if (live && r.ok) {
          setD(j);
          setError(null);
        }
      } catch {
        if (live) setError("Can't reach the Analytix server");
      }
      if (live) timer = setTimeout(load, EVERY_MS);
    };
    load();
    const tick = setInterval(() => setNow(Date.now()), 1000);
    return () => {
      live = false;
      clearTimeout(timer);
      clearInterval(tick);
    };
  }, []);
  return (
    <div className="app term az">
      <header className="topbar">
        <div className="brand">
          <span className="brand-name">ANALYTIX<span className="cursor" aria-hidden="true" /></span>
          <span className="brand-tag">bets · value and rollover</span>
        </div>
        <div className="header-right">
          <a className="alerts-open term-link" href="/analyzer">‹ Analyzer</a>
          <a className="alerts-open term-link" href="/terminal">‹ Terminal</a>
          <a className="alerts-open term-link" href="/">‹ Dashboard</a>
          <span className="badge" title={error || ""}>
            <span className={`dot ${error ? "critical" : "good pulse"}`} />
            {error ? "Server offline" : d?.enabled === false ? "Replay" : "Live"}
          </span>
          <span className="clock num" title="Your local time">{fmtTime(now)}</span>
        </div>
      </header>
      {!d && <div className="term-boot" role="status"><div>&gt; {error || "reading the bets"}<span className="cursor" aria-hidden="true" /></div></div>}
      {d && (
        <div className="term-grid">
          {!d.enabled && <p className="az-note term-wide">The bets suite reads its sources in live mode only.</p>}
          <div className="tw-5 term-col"><Rollover r={d.rollover} /></div>
          <div className="tw-7"><Value d={d} /></div>
          <Record rec={d.record} recent={d.flat_recent} />
          <div className="tw-6"><Patterns rows={d.patterns} /></div>
          <div className="tw-6"><Sources s={d.sources} now={now} /></div>
          <p className="az-note term-wide">
            Everything here is a paper bet until you place it yourself. Short odds are not safe odds: at 1.25 the favourite still
            loses about one game in five, and a rollover needs every one to win. Bet only what you can lose.
          </p>
        </div>
      )}
    </div>
  );
}

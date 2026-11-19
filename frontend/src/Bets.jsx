import { Fragment, useEffect, useState } from "react";
import { fmtTime } from "./format.js";

/* The bets suite (/bets, /api/bets): Nigerian bookmakers' football odds against a sharp price, history and form.
   Today's rollover pick and its paper chain, the flat value bets, the combo builder, the paper record, what history
   says, and the sources. Every bet here is a paper bet until you place it yourself. Colours: green and red for data only. */

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

// The favourite's own form, beside the comparison with its opponent: poor under 1.5 points a game or 2+ losses in 6
const losses = (f) => (f ? (f.results.match(/L/g) || []).length : 0);
const poor = (f) => !!f && (f.ppg < 1.5 || losses(f) >= 2);
const poorWhy = (f) => [f.ppg < 1.5 ? `${f.ppg.toFixed(1)} points a game` : null, losses(f) >= 2 ? `lost ${losses(f)} of its last ${f.n}` : null]
  .filter(Boolean).join(", ");
const Form = ({ x, dash = "-" }) => (x.agree
  ? <>{<span className="up">better</span>}{poor(x.fav_form) && <span className="down">, but poor</span>}</>
  : x.agree === false ? <span className="down">not better</span> : <span className="muted">{x.intl ? "price only" : dash}</span>);
const bar = (x, s) => (x.intl || (s.lower || []).includes(x.league) ? s.flat_low : s.flat[2]);

function Why({ x, s }) {
  const f = x.fav_form, o = x.opp_form, pat = x.pattern, rec = x.fav_record;
  return (
    <ul className="bt-why">
      <li>
        Sharp price says <b>{pct(x.fair_p, 1)}</b> ({x.sharp_src}). At {odd(x.odds)} on {BOOK[x.book] || x.book} that is an edge
        of <b className={tone(x.edge)}>{sgn(x.edge)}</b>.{x.pattern && x.p_hist != null ? <> History would move it to {pct(x.p_hist, 1)}
        ({sgn(x.edge_hist)}); the rule leaves that out.</> : null}
      </li>
      {x.agree_why && <li>Form: <Form x={x} dash="unclear" />, {x.agree_why}{x.agree && poor(f) ? `; on its own it's poor: ${poorWhy(f)}` : ""}.</li>}
      {f && o && <li>Last {f.n}: {x.fav} {f.results} ({f.gf.toFixed(1)} scored, {f.ga.toFixed(1)} conceded a game) · opponent {o.results} ({o.gf.toFixed(1)}, {o.ga.toFixed(1)}).</li>}
      {pat && <li>History: favourites priced {pat.range[0]}–{pat.range[1]} in {pat.scope} won {pct(pat.rate, 1)} of {pat.n.toLocaleString()} games, against {pct(pat.implied, 1)} the prices said.</li>}
      {rec && <li>{x.fav} as a clear favourite over three seasons: won {rec.won} of {rec.n} ({pct(rec.won / rec.n)}).</li>}
      {x.others?.length > 1 && <li>All books: {x.others.map((b) => `${BOOK[b.book] || b.book} ${odd(b.odds)}`).join(" · ")}.</li>}
      {s && (
        <li>
          Take it at <b>{odd((1 + bar(x, s)) / x.fair_p)}</b> or better{bar(x, s) > s.flat[2] ? `: ${pct(bar(x, s))} needed here, a ${x.intl ? "international" : "lower league"}` : ""}.
        </li>
      )}
    </ul>
  );
}

function Rollover({ r, pub, s }) {
  const c = r.chain;
  const x = r.today || r.candidate;
  const used = c.refills >= r.refills_max + 1;
  return (
    <section className="panel">
      <Top title="Today's rollover pick" facts={pub ? [] : [`chain ${c.n}`, `day ${c.day} of ${r.target}`, `refills ${c.refills} of ${r.refills_max}`]} />
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
            {!pub && <div><span className="muted">Stake (whole balance)</span><b className="num">{naira(r.today ? r.today.stake : c.balance)}</b></div>}
            {!pub && <div><span className="muted">Returns if it wins</span><b className="num up">{naira((r.today ? r.today.stake : c.balance) * x.odds)}</b></div>}
          </div>
          <Why x={x} s={s} />
          <p className="az-note">
            {r.today ? (r.today.status === "open" ? "Booked for today on paper. It settles after the game; tomorrow's pick waits for it."
              : `Settled: ${r.today.status === "won" ? "won" : "lost"} ${r.today.score || ""}.`)
              : "Best candidate right now; today's pick is booked on the next pass (every 10 minutes, from 06:00 Lagos) and then fixed."}
          </p>
        </div>
      ) : (
        <>
          <p className="az-note">Nothing qualifies right now: odds 1.15–1.35, kick-off within 30 hours, priced no worse than 2% under fair, with fresh odds{pub ? "" : " from your laptop"}.{(r.misses || []).length ? " Closest:" : ""}</p>
          {(r.misses || []).length > 0 && (
            <ul className="bt-why">
              {r.misses.map((m) => (
                <li key={m.key}><b>{pick(m)}</b> {odd(m.odds)} <span className="muted">{BOOK[m.book] || m.book} · {kick(m.t_ms)}</span>: {m.why.join("; ")}.</li>
              ))}
            </ul>
          )}
        </>
      )}
      <div className="bt-chain">
        {!pub && <div className="az-kpis">
          <div><span className="muted">Balance</span><b className="num">{naira(c.balance)}</b></div>
          <div><span className="muted">Best run</span><b className="num">{c.best} wins</b></div>
          <div><span className="muted">Spent so far</span><b className="num">{naira(r.start * (c.refills + 1))}</b></div>
          <div><span className="muted">Chance of day {r.target} from here</span><b className="num">{r.p_finish == null ? "-" : r.p_finish < 0.0001 ? "under 0.01%" : pct(r.p_finish, 2)}</b></div>
        </div>}
        {used && !pub && <p className="az-note">The six refills are used up: no new chain is started.</p>}
        {r.history.length > 0 && (
          <div className="az-scroll az-mid"><table className="az-table bt-t">
            <thead><tr><th>Day</th>{!pub && <th>Chain · leg</th>}<th>Pick</th><th className="r">Odds</th>{!pub && <th className="r">Stake</th>}<th>Result</th></tr></thead>
            <tbody>
              {r.history.map((p) => (
                <tr key={p.day}>
                  <td className="muted num">{p.day}</td>{!pub && <td className="num">{p.chain} · {p.leg}</td>}<td className="bt-name">{pick(p)}</td>
                  <td className="r num">{odd(p.odds)}</td>{!pub && <td className="r num">{naira(p.stake)}</td>}
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
  const s = d.settings;
  const [view, setView] = useState("value");
  const [open, setOpen] = useState(null);
  const rows = view === "value" ? d.value : d.near;
  const [lo, hi, edge, stake] = d.settings.flat;
  return (
    <section className="panel">
      <Top title={d.public ? "Value bets" : "Value bets, flat stake"} facts={[`${d.value.length} now`, `odds ${lo}–${hi}`, `edge ${pct(edge)}+ (${pct(d.settings.flat_low)}+ lower leagues)`, d.public ? null : `${naira(stake)} a bet`]}>
        <Seg value={view} set={setView} opts={[["value", "Value"], ["near", `Everything ${lo}–${hi}`]]} />
      </Top>
      {rows.length ? (
        <div className="az-scroll az-tall"><table className="az-table bt-t az-click">
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
                  <td title={x.agree_why || ""}><Form x={x} /></td>
                  <td className={`r ${x.pattern ? tone(x.pattern.rate - x.pattern.implied) : "muted"}`}
                    title={x.pattern ? `Favourites at this price won ${pct(x.pattern.rate, 1)} of ${x.pattern.n.toLocaleString()} games, priced at ${pct(x.pattern.implied, 1)}` : ""}>
                    {x.pattern ? (x.pattern.rate >= x.pattern.implied ? "better" : "worse") : "-"}</td>
                </tr>
                {open === x.key && <tr className="bt-detail"><td colSpan={8}><Why x={x} s={s} /></td></tr>}
              </Fragment>
            ))}
          </tbody>
        </table></div>
      ) : (
        <p className="az-note">
          {d.board_n ? `None of the ${d.board_n} prices in the next ${d.show_days} days is above fair by ${pct(edge)} or more right now. "Everything" shows them all.`
            : Object.values(d.matched || {}).some((n) => n > 0) ? `Your books' games matched their sharp lines, but none kicks off in the next ${d.show_days} days with fresh odds. Check the collector is still running.`
            : Object.keys(d.sources.books).length ? "Your books' games haven't matched a sharp line yet: the sharp prices may still be loading (a few minutes after a restart)."
            : d.public ? "No prices in from the Nigerian books right now."
            : "No Nigerian odds received yet. Run the collector on your laptop (see Sources), and the board fills within a minute."}
        </p>
      )}
    </section>
  );
}

const WINDOWS = [["1", "24 hours"], ["3", "3 days"], ["7", "7 days"]];
const SAVED = "analytix.bets.combo";
const saved = () => {
  try {
    return JSON.parse(localStorage.getItem(SAVED)) || {};
  } catch {
    return {};
  }
};
const chance = (p) => (p == null ? "-" : pct(p, p < 0.01 ? 2 : 1));

function Combo({ stamp }) {
  const [target, setTarget] = useState(() => saved().target || "50");
  const [days, setDays] = useState(() => saved().days || "1");
  const [c, setC] = useState(null);
  const t = Number(target);
  const ok = t >= 1.1 && t <= 200;
  useEffect(() => {
    try {
      localStorage.setItem(SAVED, JSON.stringify({ target, days }));
    } catch { /* private window: not kept */ }
    if (!ok) return undefined;
    let live = true;
    const h = setTimeout(async () => {
      try {
        const r = await fetch(`/api/bets/combo?target=${t}&days=${days}`);
        const j = await r.json();
        if (live && r.ok) setC(j);
      } catch { /* the header shows when the server is down */ }
    }, 250);
    return () => {
      live = false;
      clearTimeout(h);
    };
  }, [t, ok, target, days, stamp]);
  const s = c?.slip;
  const g = c?.settings;
  const past = (r) => r.past && r.past.n ? r.past : null;
  return (
    <section className="panel term-wide">
      <Top title="Combo builder" facts={c ? [`${c.bets} bets`, `odds ${odd(g.legs[0])}–${odd(g.legs[1])} a game`, "one book a slip"] : []}>
        <label className="bt-target">
          <span className="muted">Total odds</span>
          <input type="number" inputMode="decimal" min="1.1" max="200" step="any" value={target} aria-invalid={!ok}
            onChange={(e) => setTarget(e.target.value)}
            onBlur={() => setTarget(String(Math.min(200, Math.max(1.1, Number(target) || 1.1))))} />
        </label>
        <Seg value={days} set={setDays} opts={WINDOWS} />
      </Top>
      {!c ? <p className="az-note">Finding the best slip…</p> : s ? (
        <>
          <div className="az-kpis">
            <div><span className="muted">Total odds</span><b className="num">{odd(s.odds)}</b></div>
            <div><span className="muted">Bookmaker</span><b>{BOOK[s.book] || s.book}</b></div>
            <div><span className="muted">Games</span><b className="num">{s.n}</b></div>
            <div><span className="muted">Chance of winning</span><b className="num">{chance(s.p)}</b></div>
            <div><span className="muted">Edge at this price</span><b className={`num ${tone(s.edge)}`}>{sgn(s.edge)}</b></div>
            <div><span className="muted">Last kick-off</span><b className="num">{kick(s.last_ms)}</b></div>
          </div>
          <div className="az-scroll"><table className="az-table bt-t">
            <thead><tr><th>Kick-off</th><th>Bet</th><th className="r">Odds</th><th className="r">Fair odds</th><th className="r">Edge</th><th className="r">Chance</th></tr></thead>
            <tbody>
              {s.legs.map((x) => (
                <tr key={x.key}>
                  <td className="muted num">{kick(x.t_ms)}</td>
                  <td className="bt-name"><b>{pick(x)}</b><span className="muted az-cat">{x.home} v {x.away} · {x.league_name || x.league}</span></td>
                  <td className="r num">{odd(x.odds)}</td>
                  <td className="r num muted">{odd(1 / x.p)}</td>
                  <td className={`r num ${tone(x.edge)}`}>{sgn(x.edge)}</td>
                  <td className="r num">{pct(x.p, 1)}</td>
                </tr>
              ))}
            </tbody>
          </table></div>
        </>
      ) : (
        <p className="az-note">
          {!c.bets ? "No fresh odds from the books for games in this window right now."
            : c.target > c.reach ? `The ${c.bets} bets in this window reach ${odd(c.reach)} at most.`
            : `No slip lands between ${odd(c.target)} and ${odd(c.target * (1 + g.tol))} in this window.`}
        </p>
      )}
      {c && c.bets > 0 && (
        <div className="az-scroll"><table className="az-table bt-t az-click">
          <thead><tr><th>Target</th><th className="r">Slip</th><th className="r">Games</th><th>Book</th><th className="r">Chance of winning</th><th className="r">Edge</th>
            <th className="r" title="One slip a day on three seasons of past games, at Bet365's prices">Past slips won</th>
            <th className="r" title="One slip a day on three seasons of past games, at Bet365's prices">Past return</th></tr></thead>
          <tbody>
            {c.ladder.map((r) => {
              const p = past(r);
              return (
                <tr key={r.target} onClick={() => setTarget(String(r.target))}
                  className={`${t === r.target ? "on" : ""} ${r.target === c.best_target ? "bt-lead" : ""}`}>
                  <td className="num">{r.target}</td>
                  <td className="r num">{r.n ? odd(r.odds) : "-"}</td>
                  <td className="r num">{r.n || "-"}</td>
                  <td>{r.n ? BOOK[r.book] || r.book : "-"}</td>
                  <td className="r num">{r.n ? chance(r.p) : "-"}</td>
                  <td className={`r num ${r.n ? tone(r.edge) : ""}`}>{r.n ? sgn(r.edge) : "-"}</td>
                  <td className="r num" title={p ? `the chances expected ${p.expected.toFixed(0)}` : ""}>{p ? `${p.won} of ${p.n.toLocaleString()}` : "-"}</td>
                  <td className="r num">{p && p.n > 1 ? (p.se > 0
                    ? <><span className={Math.abs(p.roi) >= 2 * p.se ? tone(p.roi) : ""}>{sgn(p.roi)}</span> <span className="muted">± {pct(p.se, 1)}</span></>
                    : <span className="muted">{sgn(p.roi, 0)}</span>) : "-"}</td>
                </tr>
              );
            })}
          </tbody>
        </table></div>
      )}
    </section>
  );
}

function Record({ rec, recent, pub }) {
  const f = rec.flat, r = rec.rollover;
  return (
    <section className="panel term-wide">
      <Top title="Paper record" facts={[`${f.open} flat bets open`, "settled from results; closing value from the closing sharp price"]} />
      <div className="az-kpis">
        <div><span className="muted">Flat bets settled</span><b className="num">{f.n}</b></div>
        <div><span className="muted">Won</span><b className="num">{f.n ? `${f.won} (${pct(f.won / f.n)})` : "-"}</b></div>
        {!pub && <div><span className="muted">Profit</span><b className={`num ${tone(f.profit)}`}>{naira(f.profit)}</b></div>}
        <div><span className="muted">Return on stakes</span><b className={`num ${tone(f.roi)}`}>{f.roi == null ? "-" : sgn(f.roi)}</b></div>
        <div><span className="muted" title="The bet's odds against the fair closing price: positive means it beat the close">Beat the close by</span><b className={`num ${tone(f.clv)}`}>{f.clv == null ? "-" : `${sgn(f.clv)} (${f.clv_n})`}</b></div>
        {!pub && <div><span className="muted">Rollover chains</span><b className="num">{r.n}</b></div>}
        <div><span className="muted">Rollover picks won</span><b className="num">{r.picks ? `${r.won} of ${r.picks}` : "-"}</b></div>
      </div>
      {recent.length > 0 && (
        <div className="az-scroll az-mid"><table className="az-table bt-t">
          <thead><tr><th>Kick-off</th><th>Bet</th><th className="r">Odds</th><th className="r">Edge then</th><th className="r">Beat the close</th><th>Result</th>{!pub && <th className="r">Profit</th>}</tr></thead>
          <tbody>
            {recent.map((b) => (
              <tr key={b.key}>
                <td className="muted num">{kick(b.t_ms)}</td><td className="bt-name"><b>{pick(b)}</b> <span className="muted">{BOOK[b.book] || b.book}</span></td>
                <td className="r num">{odd(b.odds)}</td><td className={`r num ${tone(b.edge)}`}>{sgn(b.edge)}</td>
                <td className={`r num ${tone(b.clv)}`}>{b.clv == null ? "-" : sgn(b.clv)}</td>
                <td className={b.status === "won" ? "up" : b.status === "lost" ? "down" : "muted"}>{b.status}{b.score ? ` ${b.score}` : ""}</td>
                {!pub && <td className={`r num ${tone(b.profit)}`}>{b.profit == null ? "-" : naira(b.profit)}</td>}
              </tr>
            ))}
          </tbody>
        </table></div>
      )}
    </section>
  );
}

const BOOKS_STUDY = { bet365: "One big book (Bet365)", best: "Best price anywhere" };
const METHOD = { proportional: "proportional", power: "power", shin: "Shin's" };

function Study({ s, methods, method }) {
  const books = Object.keys(s.books || {});
  const [book, setBook] = useState(null);
  const b = book && books.includes(book) ? book : books[0];
  const x = b && s.books[b];
  const sure = (r, v) => r.se != null && Math.abs(v) >= 2 * r.se;            // two standard errors from zero
  const ret = (r) => r.n > 1 ? <><span className={sure(r, r.roi) ? tone(r.roi) : ""}>{sgn(r.roi)}</span> <span className="muted">± {pct(r.se, 1)}</span></> : "-";
  const run = (r) => r && r.n ? `${r.n} picks, ${r.won} won (${pct(r.won / r.n)}); longest run ${r.longest}, runs of 10+ ${r.to10}, runs of 30 ${r.to30}` : "no picks";
  const ms = Object.entries(methods || {}).filter(([, v]) => v.n);
  const fav = ms.length ? ms[0][1] : null;
  const won = fav && fav.fav_n ? fav.fav_won / fav.fav_n : null;
  const seasons = s.seasons || [];
  return (
    <section className="panel term-wide">
      <Top title="The rules on past games" facts={[s.games ? `${s.games.toLocaleString()} games` : null,
        seasons.length ? `${seasons[0]}/${String(seasons[0] + 1).slice(2)} to now` : null]}>
        {books.length > 1 && <Seg value={b} set={setBook} opts={books.map((k) => [k, BOOKS_STUDY[k] || k])} />}
      </Top>
      {x ? (
        <>
          <div className="az-scroll"><table className="az-table bt-t">
            <thead><tr><th>Rule</th><th className="r">Bets</th><th className="r">Won</th><th className="r">Avg odds</th><th className="r">Return a bet</th>
              <th className="r" title="The price against Pinnacle's fair closing price: above zero means it beat the close">Beat the close</th></tr></thead>
            <tbody>
              {x.rules.map((r, i) => (
                <tr key={r.rule} className={i === 0 ? "bt-lead" : ""}>
                  <td>{r.rule}</td><td className="r num muted">{r.n.toLocaleString()}</td>
                  <td className="r num">{r.n ? pct(r.won / r.n, 1) : "-"}</td><td className="r num muted">{r.n ? odd(r.odds) : "-"}</td>
                  <td className="r num">{ret(r)}</td><td className={`r num ${r.clv == null ? "" : tone(r.clv)}`}>{r.clv == null ? "-" : sgn(r.clv)}</td>
                </tr>
              ))}
            </tbody>
          </table></div>
          <p className="az-note">
            Rollover, one pick a day by today's rule: {run(x.rollover)}. With the form check: {run(x.rollover_form)}.
            {won != null && <> Favourites priced 1.15–1.50 actually won {pct(won, 1)} of {fav.fav_n.toLocaleString()} games.</>}
          </p>
        </>
      ) : <p className="az-note">Worked out each time the history is read: a minute or so after the history arrives.</p>}
      {ms.length > 0 && (
        <div className="az-scroll"><table className="az-table bt-t">
          <thead><tr><th>Taking the margin out</th><th className="r" title="Average surprise at the actual result: lower fits better">Fit (log loss)</th>
            <th className="r">Said favourites at 1.15–1.50 win</th>
            <th className="r" title={won == null ? "" : `They won ${pct(won, 1)} of ${fav.fav_n.toLocaleString()} games`}>Off by</th></tr></thead>
          <tbody>
            {ms.map(([m, v]) => {
              const said = v.fav_n ? v.fav_said / v.fav_n : null;
              return (
                <tr key={m}>
                  <td>{METHOD[m] || m}{m === method ? <b> · in use</b> : null}</td><td className="r num">{v.logloss.toFixed(5)}</td>
                  <td className="r num">{pct(said, 1)}</td><td className="r num muted">{said == null || won == null ? "-" : sgn(said - won)}</td>
                </tr>
              );
            })}
          </tbody>
        </table></div>
      )}
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
      <Top title="What history says" facts={["three seasons, 14 leagues"]}>
        {kinds.length > 1 && <Seg value={k} set={setKind} opts={[["sharp", "Pinnacle's prices"], ["avg", "Market average"]].filter(([x]) => kinds.includes(x))} />}
      </Top>
      {rows.length ? (
        <div className="az-scroll az-mid"><table className="az-table bt-t">
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
      ) : <p className="az-note">The history is read once a day; the first read takes a few minutes after the server starts.</p>}
    </section>
  );
}

function Sources({ s, now, d }) {
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
            <span className="muted">{v.games} games, {(d.matched || {})[k] ?? "-"} matched · sent {ago(now - v.t_ms)}{now - v.t_ms >= 2 * 3600_000 ? " (too old to bet on)" : ""}</span>
          </div>
        )) : (
          <div><i className="az-dot" /><b>Nigerian books</b><span className="muted">nothing received yet</span></div>
        )}
      </div>
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
          {d && !d.public && <>
            <a className="alerts-open term-link" href="/analyzer">‹ Analyzer</a>
            <a className="alerts-open term-link" href="/terminal">‹ Terminal</a>
            <a className="alerts-open term-link" href="/">‹ Dashboard</a>
          </>}
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
          <div className="tw-5 term-col"><Rollover r={d.rollover} pub={d.public} s={d.settings} /></div>
          <div className="tw-7"><Value d={d} /></div>
          <Combo stamp={d.now_ms} />
          {!d.public && <Record rec={d.record} recent={d.flat_recent} pub={d.public} />}
          {d.study && <Study s={d.study} methods={d.methods} method={d.method} />}
          <div className={d.public ? "term-wide" : "tw-6"}><Patterns rows={d.patterns} /></div>
          {!d.public && <div className="tw-6"><Sources s={d.sources} now={now} d={d} /></div>}
        </div>
      )}
    </div>
  );
}

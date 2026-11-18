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

const BAR = 0.02;                                   // the edge a value bet needs
const pinEdge = (x) => x.odds * x.fair_p - 1;      // on Pinnacle's price alone, before the history adjustment
const src = (x) => (x.sharp_src || "the sharp price").replace(/ \((The Odds API|football-data)\)$/, "");
const priceOf = (x) => { const n = src(x); return n === "Pinnacle" ? "Pinnacle's price" : n === "Betfair exchange" ? "the Betfair exchange price" : `the sharp price (${n})`; };
// A row in plain words: [word, colour class, why]
function verdict(x) {
  const e0 = pinEdge(x);
  if (e0 > 0.10) return ["Check team news", "down", "more than 10% above Pinnacle: usually a missed injury or a mismatched game"];
  if (e0 >= BAR) return ["Value", "up", `beats ${priceOf(x)} by ${pct(BAR)} or more on its own`];
  if (x.edge >= BAR) return ["History only", "", `value only once history is counted; on ${priceOf(x)} alone the edge is ${sgn(e0)}`];
  if (e0 >= -BAR) return ["Fair price", "muted", `within ${pct(BAR)} of ${priceOf(x)}`];
  return ["Poor price", "down", `more than ${pct(BAR)} below ${priceOf(x)}`];
}
const formWord = (x) => (x.agree ? ["better", "up"] : x.agree === false ? ["not better", "down"] : x.intl ? ["not checked", "muted"] : ["too few games", "muted"]);
const ppg = (f) => (f ? `${f.ppg.toFixed(1)} points a game` : "");

function Why({ x }) {
  const f = x.fav_form, o = x.opp_form, pat = x.pattern, rec = x.fav_record;
  const [word, cls, why] = verdict(x);
  const opp = x.fav === x.home ? x.away : x.home;
  const recRate = rec ? rec.won / rec.n : null;
  return (
    <ul className="bt-why">
      <li><b className={cls}>{word}</b>: {why}.</li>
      <li>
        {src(x)}: <b>{pct(x.fair_p, 1)}</b> chance, fair odds <b>{odd(1 / x.fair_p)}</b>. {BOOK[x.book] || x.book} pays <b>{odd(x.odds)}</b>:
        edge <b className={tone(pinEdge(x))}>{sgn(pinEdge(x))}</b>.
      </li>
      <li>
        {x.intl ? "History: none for internationals, so the price stands as it is."
          : pat ? <>History: favourites at fair odds {pat.range[0]}–{pat.range[1]} ({pat.scope}) won <b>{pct(pat.rate, 1)}</b> of {pat.n.toLocaleString()} games
              when priced at {pct(pat.implied, 1)}. That moves the chance to <b>{pct(x.p, 1)}</b>: edge <b className={tone(x.edge)}>{sgn(x.edge)}</b>.</>
          : "History: too few past games at this price to adjust it."}
      </li>
      {f && o ? (
        <li>
          Recent form, last {f.n}: {x.fav} {f.results.split("").join(" ")} ({ppg(f)}) · {opp} {o.results.split("").join(" ")} ({ppg(o)}).{" "}
          <span className={formWord(x)[1]}>{x.fav}'s is {formWord(x)[0]}</span>{x.agree === false ? `: ${x.agree_why}` : ""}.
        </li>
      ) : x.agree_why ? <li>Recent form: {x.agree_why}.</li> : null}
      {rec && (
        <li>
          {x.fav} as a clear favourite: won {rec.won} of {rec.n} ({pct(recRate)})
          {pat && rec.n >= 10 && Math.abs(recRate - pat.rate) >= 0.05 ? `, ${recRate < pat.rate ? "below" : "above"} the ${pct(pat.rate, 1)} for its price` : ""}.
        </li>
      )}
      <li>
        Take it at <b>{odd((1 + BAR) / x.fair_p)}</b> or better for value on {priceOf(x)} alone
        {pat ? <> (<b>{odd((1 + BAR) / x.p)}</b> counting history)</> : null}.
      </li>
      {x.others?.length > 1 && <li>All books: {x.others.map((b) => `${BOOK[b.book] || b.book} ${odd(b.odds)}`).join(" · ")}.</li>}
    </ul>
  );
}

function Rollover({ r, pub }) {
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
            <div><span className="muted">Edge with history</span><b className={`num ${tone(x.edge)}`}>{sgn(x.edge)}</b></div>
            <div><span className="muted">Edge before history</span><b className={`num ${tone(pinEdge(x))}`}>{sgn(pinEdge(x))}</b></div>
            {!pub && <div><span className="muted">Stake (whole balance)</span><b className="num">{naira(r.today ? r.today.stake : c.balance)}</b></div>}
            {!pub && <div><span className="muted">Returns if it wins</span><b className="num up">{naira((r.today ? r.today.stake : c.balance) * x.odds)}</b></div>}
          </div>
          <Why x={x} />
          <p className="az-note">
            {r.today ? (r.today.status === "open" ? "Booked for today on paper. It settles after the game; tomorrow's pick waits for it."
              : `Settled: ${r.today.status === "won" ? "won" : "lost"} ${r.today.score || ""}.`)
              : "Best candidate right now; today's pick is booked on the next pass (every 10 minutes, from 06:00 Lagos) and then fixed."}
          </p>
        </div>
      ) : (
        <div className="bt-miss">
          <p className="az-note">{(r.misses || []).length ? "Nothing qualifies right now. Closest:" : "Nothing qualifies right now, and no game in the next 30 hours comes close."}</p>
          {(r.misses || []).length > 0 && (
            <ul className="bt-why">
              {r.misses.map((m) => (
                <li key={m.key}><b>{pick(m)}</b> {odd(m.odds)} <span className="muted">{BOOK[m.book] || m.book} · {kick(m.t_ms)}</span>: {m.why.join("; ")}.</li>
              ))}
            </ul>
          )}
        </div>
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
          <div className="az-scroll az-mid"><table className="az-table">
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

const TIER = { "Value": 0, "Check team news": 1, "History only": 2, "Fair price": 3, "Poor price": 4 };

function Value({ d, now }) {
  const [view, setView] = useState("value");
  const [open, setOpen] = useState(null);
  const [lo, hi, , stake] = d.settings.flat;
  const rows = [...(view === "value" ? d.value : d.near)]
    .sort((a, b) => TIER[verdict(a)[0]] - TIER[verdict(b)[0]] || pinEdge(b) - pinEdge(a));
  const strong = d.value.filter((x) => verdict(x)[0] === "Value").length;
  const newest = Math.max(0, ...Object.values(d.sources.books || {}).map((v) => v.t_ms));
  const stale = newest && now - newest >= 2 * 3600_000;
  return (
    <section className="panel">
      <Top title="Value bets" facts={[`${d.value.length} now`, `${strong} on Pinnacle's price alone`, `odds ${lo}–${hi}`, d.public ? null : `${naira(stake)} a bet`]}>
        <Seg value={view} set={setView} opts={[["value", "Value"], ["near", `Everything ${lo}–${hi}`]]} />
      </Top>
      {rows.length ? (
        <div className="az-scroll az-tall bt-wrap"><table className="az-table az-click bt-cards">
          <thead><tr><th>Bet</th><th className="r">Odds</th><th className="r" title="Pinnacle's fair odds, its margin taken out">Pinnacle</th>
            <th className="r" title="Edge on Pinnacle's price alone">Edge</th><th className="r" title="Edge once history adjusts Pinnacle's price">With history</th>
            <th title="Is the favourite's recent form better than the opponent's?">Form</th><th>Verdict</th></tr></thead>
          <tbody>
            {rows.map((x) => {
              const [word, cls] = verdict(x), [fw, fc] = formWord(x);
              return (
                <Fragment key={x.key}>
                  <tr onClick={() => setOpen(open === x.key ? null : x.key)} className={open === x.key ? "on" : ""}>
                    <td className="bt-name bt-full"><b>{pick(x)}</b><span className="muted az-cat">{x.home} v {x.away} · {x.league_name || x.league} · {kick(x.t_ms)}</span></td>
                    <td className="r num"><b>{odd(x.odds)}</b> <span className="muted">{BOOK[x.book] || x.book}</span></td>
                    <td className="r num muted" data-k="Pinnacle">{odd(1 / x.fair_p)}</td>
                    <td className={`r num ${tone(pinEdge(x))}`} data-k="Edge">{sgn(pinEdge(x))}</td>
                    <td className={`r num ${tone(x.edge)}`} data-k="with history">{x.pattern ? sgn(x.edge) : "-"}</td>
                    <td className={fc} data-k="Form">{fw}</td>
                    <td className={`bt-verdict ${cls}`}>{word}</td>
                  </tr>
                  {open === x.key && <tr className="bt-detail"><td colSpan={7}><Why x={x} /></td></tr>}
                </Fragment>
              );
            })}
          </tbody>
        </table></div>
      ) : (
        <p className="az-note">
          {stale ? `The Nigerian odds are ${ago(now - newest).replace(" ago", "")} old, and value bets need odds under 2 hours old.${d.public ? "" : " Start the collector on your laptop."}`
            : d.board_n ? `None of the ${d.board_n} prices in the next ${d.show_days} days is ${pct(BAR)} or more above fair right now. "Everything" shows them all.`
            : Object.values(d.matched || {}).some((n) => n > 0) ? `Your books' games matched their sharp lines, but none kicks off in the next ${d.show_days} days with fresh odds. Check the collector is still running.`
            : Object.keys(d.sources.books).length ? "Your books' games haven't matched a sharp line yet: the sharp prices may still be loading (a few minutes after a restart)."
            : d.public ? "No prices in from the Nigerian books right now."
            : "No Nigerian odds received yet. Run the collector on your laptop (see Sources), and the board fills within a minute."}
        </p>
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
        <div className="az-scroll az-mid"><table className="az-table">
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

// Is a rule's return more than luck? Two standard errors from zero, and enough bets to say.
function proof(r) {
  if (!r.n || r.n < 30) return ["too few bets", "muted"];
  if (r.se != null && Math.abs(r.roi) >= 2 * r.se) return r.roi > 0 ? ["yes, a profit", "up"] : ["yes, a loss", "down"];
  return ["could be luck", "muted"];
}
const per100 = (v) => (v == null ? "-" : `${v > 0 ? "+" : v < 0 ? "−" : ""}₦${Math.abs(v * 100).toFixed(0)}`);

function reading(lead, plain) {
  if (!lead || lead.n < 30) return `Too few bets to judge the flat rule on this book yet: ${lead ? lead.n : 0} in these seasons, and about 50 are needed.`;
  const [p] = proof(lead);
  const head = `The flat rule ${lead.roi >= 0 ? "made" : "lost"} ₦${Math.abs(lead.roi * 100).toFixed(0)} per ₦100 over ${lead.n} bets: ${
    p === "could be luck" ? "that could still be luck" : "more than luck"}.`;
  if (!plain || plain.n < 30) return `${head} Too few bets yet to tell whether the history adjustment helps.`;
  const better = lead.roi > plain.roi;
  return `${head} The history adjustment ${better ? "helped" : "didn't help"}: ${per100(lead.roi)} per ₦100 with it, ${per100(plain.roi)} without.`;
}

function Study({ s, methods, method }) {
  const books = Object.keys(s.books || {});
  const [book, setBook] = useState(null);
  const b = book && books.includes(book) ? book : books[0];
  const x = b && s.books[b];
  const run = (r) => (r && r.n ? `won ${r.won} of ${r.n} (${pct(r.won / r.n)}); longest winning run ${r.longest} days of the ${30} needed` : "no picks");
  const ms = Object.entries(methods || {}).filter(([, v]) => v.n);
  const fav = ms.length ? ms[0][1] : null;
  const won = fav && fav.fav_n ? fav.fav_won / fav.fav_n : null;
  const seasons = s.seasons || [];
  return (
    <section className="panel term-wide">
      <Top title="The rules on past games" facts={[s.games ? `${s.games.toLocaleString()} games` : null,
        seasons.length ? `since ${seasons[0]}/${String(seasons[0] + 1).slice(2)}` : null]}>
        {books.length > 1 && <Seg value={b} set={setBook} opts={books.map((k) => [k, BOOKS_STUDY[k] || k])} />}
      </Top>
      {x ? (
        <>
          <p className="az-note bt-reading">{reading(x.rules[0], x.rules[1])}</p>
          <div className="az-scroll"><table className="az-table bt-cards">
            <thead><tr><th>Rule</th><th className="r">Bets</th><th className="r">Won</th><th className="r" title="One over the average odds">Needed to break even</th>
              <th className="r">Per ₦100 staked</th><th>More than luck?</th>
              <th className="r" title="Share of bets priced above Pinnacle's fair closing price">Beat the closing price</th></tr></thead>
            <tbody>
              {x.rules.map((r, i) => {
                const [pw, pc] = proof(r);
                return (
                  <tr key={r.rule} className={i === 0 ? "bt-lead" : ""}>
                    <td className="bt-full">{r.rule}</td><td className="r num muted" data-k="Bets">{r.n.toLocaleString()}</td>
                    <td className="r num" data-k="Won">{r.n ? pct(r.won / r.n, 1) : "-"}</td>
                    <td className="r num muted" data-k="needed">{r.n ? pct(1 / r.odds, 1) : "-"}</td>
                    <td className={`r num ${pc === "muted" ? "" : pc}`} data-k="Per ₦100">{r.n > 1 ? per100(r.roi) : "-"}</td>
                    <td className={`bt-proof ${pc}`}>{r.n ? pw : "-"}</td>
                    <td className="r num muted" data-k="Beat the close">{r.clv_pos == null ? "-" : pct(r.clv_pos)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table></div>
          <p className="az-note">
            Rollover rule, one pick a day: {run(x.rollover)}. Without the form check: {run(x.rollover_no_form)}.
            {won != null && <> Favourites priced 1.15–1.50 won {pct(won, 1)} of {fav.fav_n.toLocaleString()} games.</>}
          </p>
        </>
      ) : <p className="az-note">Worked out each time the history is read: a minute or so after the history arrives.</p>}
      {ms.length > 0 && (
        <div className="az-scroll"><table className="az-table bt-cards">
          <thead><tr><th>Way of taking the margin out</th><th className="r" title="Average surprise at the actual result">Fit (lower is better)</th>
            <th className="r">Said favourites at 1.15–1.50 win</th><th className="r">Off by</th></tr></thead>
          <tbody>
            {ms.map(([m, v]) => {
              const said = v.fav_n ? v.fav_said / v.fav_n : null;
              return (
                <tr key={m}>
                  <td className="bt-full">{METHOD[m] || m}{m === method ? <b> · in use</b> : null}</td><td className="r num" data-k="Fit">{v.logloss.toFixed(5)}</td>
                  <td className="r num" data-k="Said">{pct(said, 1)}</td><td className="r num muted" data-k="Off by">{said == null || won == null ? "-" : sgn(said - won)}</td>
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
  const games = rows.reduce((t, r) => t + r.n, 0);
  return (
    <section className="panel">
      <Top title="What history says" facts={[games ? `${games.toLocaleString()} bets counted` : null]}>
        {kinds.length > 1 && <Seg value={k} set={setKind} opts={[["sharp", "Pinnacle's prices"], ["avg", "Market average"]].filter(([x]) => kinds.includes(x))} />}
      </Top>
      {rows.length ? (
        <div className="az-scroll az-mid bt-wrap"><table className="az-table bt-cards">
          <thead><tr><th>Bet</th><th>Fair odds</th><th className="r">Games</th><th className="r">Won</th><th className="r">Prices said</th>
            <th className="r">Difference</th><th>More than luck?</th></tr></thead>
          <tbody>
            {rows.map((r) => {
              const diff = r.rate - r.implied;
              const real = Math.abs(diff) >= 2 * Math.sqrt((r.implied * (1 - r.implied)) / r.n);   // two standard errors
              return (
                <tr key={`${r.market}${r.odds[0]}`}>
                  <td className="bt-full">{r.name}<span className="bt-ph"> at {r.odds[0].toFixed(2)}–{r.odds[1].toFixed(2)}</span></td>
                  <td className="num bt-hide">{r.odds[0].toFixed(2)}–{r.odds[1].toFixed(2)}</td>
                  <td className="r num muted" data-k="Games">{r.n.toLocaleString()}</td>
                  <td className="r num" data-k="Won">{pct(r.rate, 1)}</td><td className="r num muted" data-k="prices said">{pct(r.implied, 1)}</td>
                  <td className={`r num ${real ? tone(diff) : "muted"}`}>{sgn(diff)}</td>
                  <td className={`bt-proof ${real ? tone(diff) : "muted"}`}>{real ? "yes" : "no"}</td>
                </tr>
              );
            })}
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
          <div className="tw-5 term-col"><Rollover r={d.rollover} pub={d.public} /></div>
          <div className="tw-7"><Value d={d} now={now} /></div>
          {!d.public && <Record rec={d.record} recent={d.flat_recent} pub={d.public} />}
          {d.study && <Study s={d.study} methods={d.methods} method={d.method} />}
          <div className={d.public ? "term-wide" : "tw-6"}><Patterns rows={d.patterns} /></div>
          {!d.public && <div className="tw-6"><Sources s={d.sources} now={now} d={d} /></div>}
        </div>
      )}
    </div>
  );
}

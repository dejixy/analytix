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
const MARKET = { 1: "Home win", X: "Draw", 2: "Away win", "1X": "Home or draw", 12: "Either team wins", X2: "Away or draw",
  O25: "Over 2.5 goals", U25: "Under 2.5 goals" };
const GOALS = ["O25", "U25"];
// The combo builder's other markets, in words (codes as in engine/goals.py and engine/corners.py)
const more = (x) => {
  const m = x.market;
  if (m === "DNB1" || m === "DNB2") return `${m === "DNB1" ? x.home : x.away} (draw no bet)`;
  if (/^PS\d+$/.test(m)) {
    const n = (x.player || "").match(/^([^,(]+),\s*([^(]+?)\s*(\(.+\))?$/);          // "Foden, Phil (Man City)"
    return `${n ? `${n[2]} ${n[1]}${n[3] ? ` ${n[3]}` : ""}` : x.player || "A player"} to score`;
  }
  const up = m.match(/^UP([12])([12])$/);
  if (up) return `${up[2] === "1" ? x.home : x.away} ${up[1]}Up`;
  if (m === "SUBN") return "Substitution not to score";
  if (m === "SUBY") return "Substitution to score";
  if (m === "GG") return "Both teams to score: yes";
  if (m === "NG") return "Both teams to score: no";
  let r = m.match(/^([OU])(\d)5$/);
  if (r) return `${r[1] === "O" ? "Over" : "Under"} ${r[2]}.5 goals`;
  r = m.match(/^([HA])([OU])(\d)5$/);
  if (r) {
    const team = r[1] === "H" ? x.home : x.away, k = Number(r[3]);
    return r[2] === "O" ? (k === 0 ? `${team} to score` : `${team} ${k + 1}+ goals`) : (k === 0 ? `${team} not to score` : `${team} under ${k}.5 goals`);
  }
  r = m.match(/^([12X])\|O(\d)5$/);
  if (r) return `${r[1] === "1" ? x.home : r[1] === "2" ? x.away : "Draw"} or over ${r[2]}.5 goals`;
  r = m.match(/^AH([12])([MP])(\d)(\d)$/);
  if (r) {
    const line = Number(r[3]) + Number(r[4]) / 10;
    return `${r[1] === "1" ? x.home : x.away} ${r[2] === "M" ? "−" : "+"}${Number.isInteger(line) ? line : line.toFixed(1)} (Asian handicap)`;
  }
  r = m.match(/^EH(\d)(\d)([12X])$/);
  if (r) {
    const start = `${r[1]}:${r[2]}`;
    return r[3] === "X" ? `Draw with ${start} handicap` : `${r[3] === "1" ? x.home : x.away} with ${start} handicap`;
  }
  r = m.match(/^(C|SH|ST|SV)([HA]?)([OU])(\d+)5$/);
  if (r) {
    const what = { C: "corners", SH: "shots", ST: "shots on target", SV: "saves" }[r[1]];
    const side = `${r[3] === "O" ? "over" : "under"} ${r[4]}.5 ${what}`;
    if (r[1] === "SV") return r[2] ? `${r[2] === "H" ? x.home : x.away} keeper ${side}` : `Keepers ${side}`;
    return r[2] ? `${r[2] === "H" ? x.home : x.away} ${side}` : `${side[0].toUpperCase()}${side.slice(1)}`;
  }
  return null;
};
// The model behind a combo leg that isn't priced from Pinnacle directly
const MODEL = { goals: "goals model", corners: "corners model", shots: "shots model", sot: "shots model", saves: "saves model",
  subs: "substitutes model" };
const pick = (x) => (x.market === "1" ? x.home : x.market === "2" ? x.away : x.market === "1X" ? `${x.home} or draw (double chance)`
  : x.market === "X2" ? `${x.away} or draw (double chance)` : x.market === "X" ? "Draw" : GOALS.includes(x.market) ? MARKET[x.market]
  : x.market === "12" ? `${x.home} or ${x.away} (double chance)` : more(x) || x.market);
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
  : x.agree === false ? <span className="down">not better</span> : <span className="muted">{x.intl || GOALS.includes(x.market) ? "price only" : dash}</span>);
const bar = (x, s) => (x.intl || (s.lower || []).includes(x.league) || GOALS.includes(x.market) ? s.flat_low : s.flat[2]);
// Value by either rule: the new one goes by Pinnacle's chance alone, the old one (before PR #140) by the history-adjusted chance
const ruled = (x) => (x.rule === "old" ? { p: x.p_hist, edge: x.edge_hist } : { p: x.p, edge: x.edge });
const Rule = ({ x }) => (x.rule ? (
  <span className="az-flag muted" title={x.rule === "old" ? "Old rule: Pinnacle's chance moved by history" : "New rule: Pinnacle's chance alone"}>
    {x.rule === "old" ? "old rule" : "new rule"}
  </span>) : null);

function Why({ x, s }) {
  const f = x.fav_form, o = x.opp_form, pat = x.pattern, rec = x.fav_record;
  return (
    <ul className="bt-why">
      <li>
        Sharp price says <b>{pct(x.fair_p, 1)}</b> ({x.sharp_src}). At {odd(x.odds)} on {BOOK[x.book] || x.book} that is an edge
        of <b className={tone(x.edge)}>{sgn(x.edge)}</b>.{x.pattern && x.p_hist != null ? <> {x.rule === "old" ? "History moves it to" : "History would move it to"} {pct(x.p_hist, 1)}{" "}
        ({sgn(x.edge_hist)}){x.rule === "old" ? ": the old rule goes by that" : "; the new rule leaves that out"}.</> : null}
      </li>
      {x.agree_why && <li>Form: <Form x={x} dash="unclear" />, {x.agree_why}{x.agree && poor(f) ? `; on its own it's poor: ${poorWhy(f)}` : ""}.</li>}
      {f && o && <li>Last {f.n}: {x.fav} {f.results} ({f.gf.toFixed(1)} scored, {f.ga.toFixed(1)} conceded a game) · opponent {o.results} ({o.gf.toFixed(1)}, {o.ga.toFixed(1)}).</li>}
      {pat && <li>History: favourites priced {pat.range[0]}–{pat.range[1]} in {pat.scope === x.league ? x.league_name || pat.scope : pat.scope} won {pct(pat.rate, 1)} of {pat.n.toLocaleString()} games, against {pct(pat.implied, 1)} the prices said.</li>}
      {rec && (
        <li>
          {x.fav} as a clear favourite over three seasons: won {rec.won} of {rec.n} ({pct(rec.won / rec.n)})
          {(x.market === "1X" || x.market === "X2") && rec.drew != null ? `; won or drew ${rec.won + rec.drew} of ${rec.n} (${pct((rec.won + rec.drew) / rec.n)})` : ""}.
        </li>
      )}
      {x.others?.length > 1 && <li>All books: {x.others.map((b) => `${BOOK[b.book] || b.book} ${odd(b.odds)}`).join(" · ")}.</li>}
      {s && (
        <li>
          Take it at <b>{odd((1 + bar(x, s)) / (x.rule === "old" ? x.p_hist : x.fair_p))}</b> or better{x.rule === "old" ? " on the old rule" : ""}{bar(x, s) > s.flat[2] ? `: ${pct(bar(x, s))} needed here, ${x.intl ? "an international"
            : GOALS.includes(x.market) ? "a goals bet" : "a lower league"}` : ""}.
        </li>
      )}
    </ul>
  );
}

// The rollover side: your rollover, George's or Muiz's, one at a time, picked with the toggle in the box's title row
const ROLL_VIEW = "analytix.bets.rollview";
function RollSide({ d }) {
  const [view, setView] = useState(() => {
    try {
      return localStorage.getItem(ROLL_VIEW) || "mine";
    } catch {
      return "mine";
    }
  });
  const choose = (v) => {
    setView(v);
    try {
      localStorage.setItem(ROLL_VIEW, v);
    } catch { /* private window: not kept */ }
  };
  const lanes = d.lanes || [];
  const lane = lanes.find((l) => l.key === view);
  const opts = [["mine", "Rollover"], ...lanes.map((l) => [l.key, l.name.replace(/ Section$/, "")])];
  const toggle = lanes.length ? (
    <span className="az-seg" role="group" aria-label="Whose rollover">
      {opts.map(([k, label]) => (
        <button key={k} className={(lane ? lane.key : "mine") === k ? "on" : ""} aria-pressed={(lane ? lane.key : "mine") === k}
          onClick={() => choose(k)}>{label}</button>
      ))}
    </span>
  ) : null;
  return lane ? <Rollover r={lane} pub={d.public} s={d.settings} title={lane.name} head={toggle} />
    : <Rollover r={d.rollover} pub={d.public} s={d.settings} head={toggle} />;
}

// A friend's rollover leg: a slip of several of today's games
function SlipLeg({ x, r, c, pub }) {
  const open = r.today && r.today.status === "open";
  const stake = r.today ? r.today.stake : c.balance;
  return (
    <div className="bt-pick">
      <div className="bt-pick-top">
        <div>
          <div className="bt-pick-name">{x.n} games</div>
          <div className="muted">last kick-off {kick(x.last_ms || x.t_ms)}</div>
        </div>
        <div className="bt-pick-odds">
          <b className="num">{odd(x.odds)}</b>
          <span className="muted">{BOOK[x.book] || x.book}</span>
        </div>
      </div>
      <div className="az-kpis">
        <div><span className="muted">Chance of winning</span><b className="num">{pct(x.p, 1)}</b></div>
        <div><span className="muted">Edge at this price</span><b className={`num ${tone(x.edge)}`}>{sgn(x.edge)}</b></div>
        <div><span className="muted">Least likely game</span><b className="num">{pct(x.least, 1)}</b></div>
        {!pub && <div><span className="muted">Stake (whole balance)</span><b className="num">{naira(stake)}</b></div>}
        {!pub && <div><span className="muted">Returns if it wins</span><b className="num up">{naira(stake * x.odds)}</b></div>}
      </div>
      {x.book === "sportybet" && <SportyCode url={`/api/bets/lane/code?lane=${r.key}`} slip={x.key} pub={pub} />}
      <div className="az-scroll"><table className="az-table bt-t">
        <thead><tr><th>Kick-off</th><th>Bet</th><th className="r">Odds</th><th className="r">Chance</th>{r.today && <th>Result</th>}</tr></thead>
        <tbody>
          {x.legs.map((g) => (
            <tr key={g.key}>
              <td className="muted num">{kick(g.t_ms)}</td>
              <td className="bt-name"><b>{pick(g)}</b><span className="muted az-cat">{g.home} v {g.away}{g.league_name || g.league ? ` · ${g.league_name || g.league}` : ""}</span></td>
              <td className="r num">{odd(g.odds)}</td>
              <td className="r num">{pct(g.p + (g.p_void || 0), 1)}</td>
              {r.today && <td className={g.status === "won" ? "up" : g.status === "lost" ? "down" : "muted"}>{g.status === "open" ? "open" : `${g.status} ${g.score || ""}`}</td>}
            </tr>
          ))}
        </tbody>
      </table></div>
      <p className="az-note">
        {r.today ? (open ? "Booked on paper. Once every game has finished (or one loses), the next slip from games later today is booked."
          : `Settled: ${r.today.status === "won" ? "won" : "lost"}. No slip from games later today lands in the range.`)
          : "Best slip right now: booked on the next pass (every 10 minutes), then fixed."}
      </p>
    </div>
  );
}

function Rollover({ r, pub, s, title = "Today's rollover pick", head = null }) {
  const c = r.chain;
  const x = r.today || r.candidate;
  const used = c.refills >= r.refills_max + 1;
  const rng = r.range || s?.roll || [1.2, 1.5];
  return (
    <section className="panel">
      <Top title={title} facts={[r.today_legs ? `${r.today_won} won today` : null,
        ...(pub ? [] : [`chain ${c.n}`, `win ${c.day} of ${r.target}`, `refills ${c.refills} of ${r.refills_max}`])]}>{head}</Top>
      {x && x.legs ? <SlipLeg x={x} r={r} c={c} pub={pub} /> : x ? (
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
            {r.today ? (r.today.status === "open" ? "Booked on paper. Once it settles, the next qualifying game later today is booked."
              : `Settled: ${r.today.status === "won" ? "won" : "lost"} ${r.today.score || ""}. Nothing else qualifies later today.`)
              : "Best candidate right now: booked on the next pass (every 10 minutes), then fixed. After it settles, the next qualifying game later today follows."}
          </p>
        </div>
      ) : (
        <>
          <p className="az-note">{r.slip ? `No slip lands between ${odd(rng[0])} and ${odd(rng[1])} right now from games later today, each priced no worse than 2% under fair, with fresh odds${pub ? "" : " from your laptop"}.`
            : <>Nothing qualifies right now: odds {odd(rng[0])}–{odd(rng[1])}, kick-off later today, priced no worse than 2% under fair, with fresh odds{pub ? "" : " from your laptop"}.{(r.misses || []).length ? " Closest:" : ""}</>}</p>
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
          <div><span className="muted">Chance of {r.target} straight wins from here</span><b className="num">{r.p_finish == null ? "-" : r.p_finish < 0.0001 ? "under 0.01%" : pct(r.p_finish, 2)}</b></div>
        </div>}
        {used && !pub && <p className="az-note">The six refills are used up: no new chain is started.</p>}
        {!pub && r.history.length > 0 && (
          <div className="az-scroll az-mid"><table className="az-table bt-t">
            <thead><tr><th>Day</th><th>Chain · leg</th><th>Pick</th><th className="r">Odds</th><th className="r">Stake</th><th>Result</th></tr></thead>
            <tbody>
              {r.history.map((p) => (
                <tr key={`${p.key}-${p.placed_ms}`}>
                  <td className="muted num">{p.day}</td><td className="num">{p.chain} · {p.leg}</td><td className="bt-name">{p.legs ? `${p.n} games` : pick(p)}</td>
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
  const s = d.settings;
  const [view, setView] = useState("value");
  const [open, setOpen] = useState(null);
  const rows = view === "value" ? d.value : d.near;
  const [lo, hi, edge, stake] = d.settings.flat;
  const olds = d.value.filter((x) => x.rule === "old").length;
  return (
    <section className="panel">
      <Top title={d.public ? "Value bets" : "Value bets, flat stake"} facts={[`${d.value.length} now${olds ? ` (${olds} on the old rule)` : ""}`, `odds ${lo}–${hi}`, `edge ${pct(edge)}+ (${pct(d.settings.flat_low)}+ lower leagues)`, d.public ? null : `${naira(stake)} a bet`]}>
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
                  <td className="bt-name"><b>{pick(x)}</b><Rule x={x} /><span className="muted az-cat">{x.home} v {x.away} · {x.league_name || x.league}</span></td>
                  <td className="r num">{odd(x.odds)} <span className="muted">{BOOK[x.book] || x.book}</span></td>
                  <td className="r num muted">{odd(1 / ruled(x).p)}</td>
                  <td className={`r num ${tone(ruled(x).edge)}`}>{sgn(ruled(x).edge)}</td>
                  <td className="r num">{pct(ruled(x).p, 1)}</td>
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
          {d.board_n ? `None of the ${d.board_n} prices in the next ${d.show_days} days is above fair by ${pct(edge)} or more right now, on either rule. "Everything" shows them all.`
            : d.public ? "No fresh prices from the Nigerian books right now."
            : Object.values(d.matched || {}).some((n) => n > 0) ? `Your books' games matched their sharp lines, but none kicks off in the next ${d.show_days} days with fresh odds. Check the collector is still running.`
            : Object.keys(d.sources.books).length ? "Your books' games haven't matched a sharp line yet: the sharp prices may still be loading (a few minutes after a restart)."
            : "No Nigerian odds received yet. Run the collector on your laptop (see Sources), and the board fills within a minute."}
        </p>
      )}
    </section>
  );
}

const WINDOWS = [["0", "Today"], ["1", "24 hours"], ["3", "3 days"], ["7", "7 days"]];
// A picked league's name, kept so the picker can still show it when that league has no games in the window
const LEAGUE_NAMES = "analytix.bets.leaguenames";
const savedName = (code) => {
  try {
    return JSON.parse(localStorage.getItem(LEAGUE_NAMES) || "{}")[code] || code;
  } catch {
    return code;
  }
};
const rememberName = (code, name) => {
  if (!code || !name) return;
  try {
    localStorage.setItem(LEAGUE_NAMES, JSON.stringify({ ...JSON.parse(localStorage.getItem(LEAGUE_NAMES) || "{}"), [code]: name }));
  } catch { /* private window: not kept */ }
};
const GAMES = [["", "auto"], ...[10, 15, 20, 25, 30].map((v) => [String(v), String(v)])];
const LEAST = [["", "safest first"], ...[60, 65, 70, 75, 80, 85, 90].map((v) => [String(v / 100), `${v}%+`])];
const KINDS = [["win", "1X2"], ["dc", "Double chance"], ["up1", "1Up"], ["up2", "2Up"], ["dnb", "Draw no bet"], ["o15", "Over 1.5 goals"], ["o25", "Over 2.5 goals"],
  ["btts", "Both teams to score"], ["mix", "Home/away or over"], ["handicap", "Handicap"], ["corners", "Corners"], ["shots", "Team shots"],
  ["saves", "Goalkeeper saves"], ["subs", "Substitution to score"], ["scorers", "Players to score"]];
const ALL_KINDS = KINDS.map(([k]) => k);
const OLD_ALL = ["win", "dnb", "goals", "team", "btts", "mix", "corners"];       // "all" before double chance had its own button
const SAVED = "analytix.bets.combo";
const saved = () => {
  try {
    return JSON.parse(localStorage.getItem(SAVED)) || {};
  } catch {
    return {};
  }
};
const chance = (p) => (p == null ? "-" : pct(p, p < 0.01 ? 2 : 1));

// A SportyBet booking code for the slip, made on the laptop (SportyBet turns the server away): asked for, then followed
// Players to score: a slip of their own, from SportyBet's shortest-priced scorers. No chance, edge or fair odds of ours.
function Scorers({ sc, q, target, pub }) {
  const s = sc.slip;
  return (
    <>
      <div className="bt-kinds"><b>Players to score</b></div>
      {!s ? (
        <p className="az-note">{sc.players ? `No set of goalscorers lands between ${odd(target)} and ${odd(target * 1.1)} in this window.`
          : "No SportyBet goalscorer odds for games in this window right now."}</p>
      ) : (
        <>
          <div className="az-kpis">
            <div><span className="muted">Total odds</span><b className="num">{odd(s.odds)}</b></div>
            <div><span className="muted">Bookmaker</span><b>{BOOK[s.book] || s.book}</b></div>
            <div><span className="muted">Players</span><b className="num">{s.n}</b></div>
            <div><span className="muted">Chance of winning</span><b className="num">--</b></div>
            <div><span className="muted">Edge at this price</span><b className="num">--</b></div>
            <div><span className="muted">Last kick-off</span><b className="num">{kick(s.last_ms)}</b></div>
          </div>
          {s.coded ? <SportyCode q={`${q}&which=scorers`} slip={s.legs.map((x) => x.key).join()} pub={pub} />
            : !pub && <div className="bt-code"><span className="muted">A SportyBet code needs the updated collector on your laptop.</span></div>}
          <div className="az-scroll"><table className="az-table bt-t">
            <thead><tr><th>Kick-off</th><th>Bet</th><th className="r">Odds</th><th className="r">Fair odds</th><th className="r">Edge</th><th className="r">Chance</th></tr></thead>
            <tbody>
              {s.legs.map((x) => (
                <tr key={x.key}>
                  <td className="muted num">{kick(x.t_ms)}</td>
                  <td className="bt-name"><b>{pick(x)}</b><span className="muted az-cat">{x.home} v {x.away}{x.league_name || x.league ? ` · ${x.league_name || x.league}` : ""}</span></td>
                  <td className="r num">{odd(x.odds)}</td>
                  <td className="r num muted">--</td>
                  <td className="r num muted">--</td>
                  <td className="r num muted">--</td>
                </tr>
              ))}
            </tbody>
          </table></div>
        </>
      )}
    </>
  );
}

function SportyCode({ q, url, slip, pub }) {
  const [job, setJob] = useState(null);
  const [copied, setCopied] = useState(false);
  const u = url || `/api/bets/combo/code?${q}`;
  useEffect(() => setJob(null), [u, slip]);
  useEffect(() => {
    if (job?.status !== "waiting") return undefined;
    const h = setTimeout(async () => {
      try {
        const r = await fetch(u);
        if (r.ok) setJob(await r.json());
      } catch { /* tried again on the next tick */ }
    }, 3000);
    return () => clearTimeout(h);
  }, [job, u]);
  const ask = async () => {
    setJob({ status: "waiting" });
    try {
      const r = await fetch(u);
      setJob(r.ok ? await r.json() : { status: "failed", why: "the server didn't answer" });
    } catch {
      setJob({ status: "failed", why: "the server didn't answer" });
    }
  };
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(job.code);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch { /* the code is on screen to type */ }
  };
  return (
    <div className="bt-code">
      {!job ? <button className="az-go" onClick={ask}>Get SportyBet code</button>
        : job.status === "waiting" ? <span className="muted">Asking SportyBet{pub ? "" : " through your laptop (it answers between reads of the books)"}…</span>
        : job.status === "ready" ? (
          <>
            <span className="muted">SportyBet code</span><b className="bt-code-v num">{job.code}</b>
            <button className="az-go" onClick={copy}>{copied ? "copied" : "copy"}</button>
            {job.url && <a className="az-go" href={job.url} target="_blank" rel="noreferrer">open on SportyBet</a>}
            {job.deadline_ms && <span className="muted">valid until {kick(job.deadline_ms)}</span>}
            {job.missing?.length > 0 && <span className="down">left out by SportyBet: {job.missing.join(", ")}</span>}
          </>
        ) : <><span className="muted">{pub ? "No code right now. Try again in a minute." : `No code: ${job.why}`}</span>
          <button className="az-go" onClick={ask}>try again</button></>}
    </div>
  );
}

const VIEWS = [["slip", "Slip"], ["mixer", "Mixer bundle"], ["vet", "Vet"]];
const TICKETS = ["2", "3", "4", "5", "6", "8", "10"];
const legName = (x) => (x.market ? pick(x) : x.label || "-");
const legSub = (x) => `${x.home} v ${x.away}${x.league_name || x.league ? ` · ${x.league_name || x.league}` : ""}`;

// The mixer bundle: several tickets at the target, no game on more than two of them
function Mixer({ q, pub }) {
  const [b, setB] = useState(null);
  useEffect(() => {
    let live = true;
    setB(null);
    const h = setTimeout(async () => {
      try {
        const r = await fetch(`/api/bets/combo/bundle?${q}`);
        const j = await r.json();
        if (live && r.ok) setB(j);
      } catch { /* the header shows when the server is down */ }
    }, 250);
    return () => {
      live = false;
      clearTimeout(h);
    };
  }, [q]);
  if (!b) return <p className="az-note">Building the bundle…</p>;
  if (!b.tickets.length) return <p className="az-note">No ticket lands at these settings in this window.</p>;
  return (
    <>
      <div className="az-kpis">
        <div><span className="muted">Tickets</span><b className="num">{b.tickets.length}{b.tickets.length < b.asked ? ` of ${b.asked}` : ""}</b></div>
        <div><span className="muted">Games used</span><b className="num">{b.games}</b></div>
        <div><span className="muted">Games on two tickets</span><b className="num">{b.shared}</b></div>
        <div><span className="muted">Most tickets one game can sink</span><b className="num">{b.most}</b></div>
        <div><span className="muted">Most games two tickets share</span><b className="num">{b.overlap}</b></div>
        <div><span className="muted">Tickets expected to win</span><b className="num">{b.expected.toFixed(2)}</b></div>
      </div>
      {b.tickets.map((s, i) => (
        <div key={s.legs.map((x) => x.key).join()}>
          <div className="bt-kinds">
            <b>Ticket {i + 1}</b>
            <span className="num">{odd(s.odds)}</span>
            <span className="muted">{s.n} games · {BOOK[s.book] || s.book}</span>
            <span className="muted">chance <b className="num">{chance(s.p)}</b></span>
            <span className="muted">edge <b className={`num ${tone(s.edge)}`}>{sgn(s.edge)}</b></span>
          </div>
          {s.book === "sportybet" && s.coded && <SportyCode q={`${q}&which=t${i + 1}`} slip={s.legs.map((x) => x.key).join()} pub={pub} />}
          <div className="az-scroll"><table className="az-table bt-t">
            <thead><tr><th>Kick-off</th><th>Bet</th><th className="r">Odds</th><th className="r">Chance</th><th>Also on</th></tr></thead>
            <tbody>
              {s.legs.map((x) => (
                <tr key={x.key}>
                  <td className="muted num">{kick(x.t_ms)}</td>
                  <td className="bt-name"><b>{pick(x)}</b>{x.model && <span className="az-flag muted" title={x.sharp_src}>{MODEL[x.model] || `${x.model} model`}</span>}
                    <span className="muted az-cat">{legSub(x)}</span></td>
                  <td className="r num">{odd(x.odds)}</td>
                  <td className="r num">{pct(x.p + (x.p_void || 0), 1)}</td>
                  <td className="muted">{x.also.length ? x.also.map((k) => `ticket ${k}`).join(", ") : "-"}</td>
                </tr>
              ))}
            </tbody>
          </table></div>
        </div>
      ))}
    </>
  );
}

// The Vet: a SportyBet ticket by its code, game by game, with what could trip it and what to do about it
const VERDICT = { keep: ["keep", "up"], swap: ["swap it", "down"], safer: ["safer bet", "down"], drop: ["take it off", "down"],
  started: ["started", "muted"], off: ["suspended", "down"], unpriced: ["can't price this bet", "muted"], unknown: ["not on the board", "muted"] };
const optText = (o) => (o.kind === "drop" ? "take it off" : `${legName(o.bet)} ${odd(o.bet.odds)} (${pct(o.bet.p, 1)})${o.kind === "swap" ? `, ${o.bet.home} v ${o.bet.away}` : ""}`);

function Vet({ code, q, pub }) {
  const [v, setV] = useState(null);
  const u = `/api/bets/vet?code=${encodeURIComponent(code)}&${q}`;
  useEffect(() => {
    if (!code) return undefined;
    let live = true;
    let h = null;
    const load = async () => {
      try {
        const r = await fetch(u);
        const j = r.ok ? await r.json() : { status: "failed", why: "the server didn't answer" };
        if (!live) return;
        setV(j);
        if (j.status === "waiting") h = setTimeout(load, 3000);
      } catch {
        if (live) h = setTimeout(load, 3000);
      }
    };
    setV({ status: "waiting" });
    load();
    return () => {
      live = false;
      clearTimeout(h);
    };
  }, [u, code]);
  if (!code) return null;
  if (!v || v.status === "waiting") return <p className="az-note">Loading {code} from SportyBet{pub ? "" : " through your laptop"}…</p>;
  if (v.status !== "ready") return <p className="az-note">{v.why || "No ticket on that code."}</p>;
  const tk = v.ticket;
  const vt = v.vetted;
  return (
    <>
      <div className="az-kpis">
        <div><span className="muted">Ticket</span><b className="num">{odd(tk.odds)}</b></div>
        <div><span className="muted">Games</span><b className="num">{tk.n}</b></div>
        <div><span className="muted">Chance of winning{tk.priced < tk.n ? ` (${tk.priced} priced)` : ""}</span><b className="num">{chance(tk.p)}</b></div>
        <div><span className="muted">Edge at this price</span><b className={`num ${tone(tk.edge)}`}>{sgn(tk.edge)}</b></div>
        <div><span className="muted">New ticket</span><b className="num">{odd(vt.odds)}</b></div>
        <div><span className="muted">Its chance{vt.priced < vt.n ? ` (${vt.priced} priced)` : ""}</span><b className={`num ${vt.p != null && tk.p != null ? tone(vt.p - tk.p) : ""}`}>{chance(vt.p)}</b></div>
        <div><span className="muted">Changes</span><b className="num">{vt.changes}</b></div>
      </div>
      {!v.fresh && <p className="az-note down">SportyBet's odds on the board are over 2 hours old: swaps may be missing.</p>}
      <div className="az-scroll"><table className="az-table bt-t">
        <thead><tr><th>Kick-off</th><th>Bet</th><th className="r">Odds</th><th className="r">Chance</th><th className="r">Edge</th><th>Verdict</th><th>Fix</th><th className="r">Ticket after</th></tr></thead>
        <tbody>
          {v.legs.map((x) => {
            const [word, cls] = VERDICT[x.verdict] || [x.verdict, "muted"];
            const fix = x.fix != null ? x.options[x.fix] : null;
            const others = x.verdict === "keep" || x.verdict === "started" ? [] : x.options.filter((o, k) => k !== x.fix);
            return (
              <tr key={`${x.sel.event}:${x.sel.market}:${x.sel.outcome}`}>
                <td className="muted num">{kick(x.t_ms)}</td>
                <td className="bt-name"><b>{legName(x)}</b>{x.model && <span className="az-flag muted" title={x.sharp_src}>{MODEL[x.model] || `${x.model} model`}</span>}
                  <span className="muted az-cat">{legSub(x)}</span></td>
                <td className="r num">{odd(x.odds)}</td>
                <td className="r num">{x.p == null ? "-" : pct(x.p, 1)}</td>
                <td className={`r num ${tone(x.edge)}`}>{x.edge == null ? "-" : sgn(x.edge)}</td>
                <td className={cls}>{word}{x.weakest ? <span className="muted"> · least likely</span> : null}{x.why.length ? <span className="muted az-cat">{x.why.join("; ")}</span> : null}</td>
                <td>{fix ? <b>{optText(fix)}</b> : "-"}{others.map((o) => <span key={o.kind} className="muted az-cat">or {optText(o)} · ticket {odd(o.odds)}, {chance(o.p)}</span>)}</td>
                <td className="r num">{fix ? <>{odd(fix.odds)} <span className="muted">{chance(fix.p)}</span></> : "-"}</td>
              </tr>
            );
          })}
        </tbody>
      </table></div>
      <NewTicket v={v} code={code} q={q} pub={pub} />
    </>
  );
}

// The new ticket: every fix in, one bet a game, what was left off and why, and its SportyBet code
const LEFT = { drop: "taken off", started: "already started", clash: "a second bet on one game" };
const CHANGE = { keep: ["kept", "muted"], swap: ["swapped in", "up"], safer: ["safer bet", "up"] };
function NewTicket({ v, code, q, pub }) {
  const vt = v.vetted;
  const changed = vt.changes > 0 || vt.left.length > 0;
  return (
    <>
      <div className="bt-kinds">
        <b>New ticket</b>
        <span className="num">{odd(vt.odds)}</span>
        <span className="muted">{vt.n} games</span>
        <span className="muted">chance <b className="num">{chance(vt.p)}</b></span>
        {!changed && <span className="muted">nothing to change: your ticket stands</span>}
      </div>
      {changed && vt.legs.length > 0 && (vt.coded ? (
        <SportyCode url={`/api/bets/vet/code?code=${encodeURIComponent(code)}&${q}`} slip={`${code}:${vt.legs.map((x) => x.sel.event + x.sel.outcome).join()}`} pub={pub} />
      ) : !pub && <div className="bt-code"><span className="muted">A SportyBet code needs the updated collector on your laptop.</span></div>)}
      {changed && (
        <div className="az-scroll"><table className="az-table bt-t">
          <thead><tr><th>Kick-off</th><th>Bet</th><th className="r">Odds</th><th className="r">Chance</th><th>On the new ticket</th></tr></thead>
          <tbody>
            {vt.legs.map((x) => {
              const [word, cls] = CHANGE[x.change] || [x.change, "muted"];
              return (
                <tr key={`${x.sel.event}:${x.sel.outcome}`}>
                  <td className="muted num">{kick(x.t_ms)}</td>
                  <td className="bt-name"><b>{legName(x)}</b><span className="muted az-cat">{legSub(x)}</span></td>
                  <td className="r num">{odd(x.odds)}</td>
                  <td className="r num">{x.p == null ? "-" : pct(x.p, 1)}</td>
                  <td className={cls}>{word}{x.was ? <span className="muted az-cat">for {legName(x.was)}, {x.was.home} v {x.was.away}</span> : null}</td>
                </tr>
              );
            })}
            {vt.left.map((x) => (
              <tr key={`left:${x.sel.event}:${x.sel.outcome}`} className="muted">
                <td className="num">{kick(x.t_ms)}</td>
                <td className="bt-name"><s>{legName(x)}</s><span className="az-cat">{legSub(x)}</span></td>
                <td className="r num">{odd(x.odds)}</td>
                <td className="r num">-</td>
                <td className="down">{LEFT[x.change] || x.change}</td>
              </tr>
            ))}
          </tbody>
        </table></div>
      )}
    </>
  );
}

function Combo({ stamp, pub }) {
  const [target, setTarget] = useState(() => saved().target || "10");
  const [days, setDays] = useState(() => saved().days || "1");
  const [least, setLeast] = useState(() => saved().least || "");
  const [book, setBook] = useState(() => saved().book || "");
  const [games, setGames] = useState(() => saved().games || "");
  const [leagues, setLeagues] = useState(() => (Array.isArray(saved().leagues) ? saved().leagues : saved().league ? [saved().league] : []));
  const flipLeague = (code, name) => {
    rememberName(code, name);
    setLeagues((ls) => (ls.includes(code) ? ls.filter((x) => x !== code) : [...ls, code]));
  };
  const [kinds, setKinds] = useState(() => {
    const k = saved().kinds;
    if (!Array.isArray(k) || !k.length || OLD_ALL.every((x) => k.includes(x))) return ALL_KINDS;    // everything stays everything
    return ALL_KINDS.filter((x) => k.includes(x) || (x === "dc" && k.includes("win"))              // wins used to carry double chance
      || ((x === "up1" || x === "up2") && k.includes("up"))                                          // 1Up and 2Up shared a button
      || ((x === "o15" || x === "o25") && k.includes("goals")));                                     // over/under goals became over 1.5 and 2.5
  });
  const flip = (k) => setKinds((ks) => (ks.includes(k) ? (ks.length > 1 ? ks.filter((x) => x !== k) : ks) : ALL_KINDS.filter((x) => x === k || ks.includes(x))));
  const [view, setView] = useState(() => (VIEWS.some(([k]) => k === saved().view) ? saved().view : "slip"));
  const [tickets, setTickets] = useState(() => saved().tickets || "5");
  const [vetIn, setVetIn] = useState("");
  const [vetCode, setVetCode] = useState("");
  const vetGo = () => setVetCode(vetIn.trim().toUpperCase());
  const [c, setC] = useState(null);
  const t = Number(target);
  const ok = t >= 1.1 && t <= 1000;
  useEffect(() => {
    try {
      localStorage.setItem(SAVED, JSON.stringify({ target, days, least, book, kinds, games, leagues, view, tickets }));
    } catch { /* private window: not kept */ }
    if (!ok) return undefined;
    let live = true;
    const h = setTimeout(async () => {
      try {
        const r = await fetch(`/api/bets/combo?target=${t}&days=${days}&least=${least || 0}&book=${book}&markets=${kinds.join(",")}&games=${games || 0}&leagues=${encodeURIComponent(leagues.join(","))}`);
        const j = await r.json();
        if (live && r.ok) setC(j);
      } catch { /* the header shows when the server is down */ }
    }, 250);
    return () => {
      live = false;
      clearTimeout(h);
    };
  }, [t, ok, target, days, least, book, kinds, games, leagues, stamp, view, tickets]);
  const s = c?.slip;
  const g = c?.settings;
  const onlyScorers = kinds.length === 1 && kinds[0] === "scorers";
  const q = `target=${t}&days=${days}&least=${least || 0}&markets=${kinds.join(",")}&games=${games || 0}&leagues=${encodeURIComponent(leagues.join(","))}`;
  const leagueList = [...(c?.leagues || [])];
  leagues.forEach((code) => { if (!leagueList.some((l) => l.code === code)) leagueList.push({ code, name: savedName(code), games: 0 }); });
  const past = (r) => r.past && r.past.n ? r.past : null;
  const floor = c && (c.least || g.floors[g.floors.length - 1]);
  return (
    <section className="panel term-wide">
      <Top title="Combo builder" facts={!c || view === "vet" ? [] : view === "mixer" ? [`${tickets} tickets`, "a game on 2 at most", `up to ${g.games} games each`]
        : onlyScorers ? [`${c.scorers?.players || 0} goalscorers`, "shortest odds first", "one a game"]
        : [`${c.bets} bets`, c.least ? `every game ${pct(c.least)}+` : "safest games first", `up to ${g.games} games`, "one book a slip"]}>
        <Seg value={view} set={setView} opts={VIEWS} />
        {view === "vet" ? (
          <>
            <label className="bt-target">
              <span className="muted">SportyBet code</span>
              <input className="bt-code-in" type="text" value={vetIn} maxLength={12} spellCheck={false} autoCapitalize="characters"
                onChange={(e) => setVetIn(e.target.value.replace(/[^a-z0-9]/gi, ""))} onKeyDown={(e) => e.key === "Enter" && vetGo()} />
            </label>
            <button className="az-go" onClick={vetGo} disabled={vetIn.trim().length < 4}>Vet</button>
          </>
        ) : <>
        <label className="bt-target">
          <span className="muted">Total odds</span>
          <input type="number" inputMode="decimal" min="1.1" max="1000" step="any" value={target} aria-invalid={!ok}
            onChange={(e) => setTarget(e.target.value)}
            onBlur={() => setTarget(String(Math.min(1000, Math.max(1.1, Number(target) || 1.1))))} />
        </label>
        <label className="bt-target">
          <span className="muted">Each game</span>
          <select className="az-select" value={least} onChange={(e) => setLeast(e.target.value)}>
            {LEAST.map(([v, label]) => <option key={v} value={v}>{label}</option>)}
          </select>
        </label>
        <label className="bt-target">
          <span className="muted">Max games</span>
          <select className="az-select" value={games} onChange={(e) => setGames(e.target.value)}>
            {GAMES.map(([v, label]) => <option key={v} value={v}>{label}</option>)}
          </select>
        </label>
        <label className="bt-target">
          <span className="muted">Book</span>
          <select className="az-select" value={book} onChange={(e) => setBook(e.target.value)}>
            <option value="">any</option>
            {[...new Set([...(c?.books || []), ...(book ? [book] : [])])].map((k) => <option key={k} value={k}>{BOOK[k] || k}</option>)}
          </select>
        </label>
        {view === "mixer" && (
          <label className="bt-target">
            <span className="muted">Tickets</span>
            <select className="az-select" value={tickets} onChange={(e) => setTickets(e.target.value)}>
              {TICKETS.map((v) => <option key={v} value={v}>{v}</option>)}
            </select>
          </label>
        )}
        <Seg value={days} set={setDays} opts={WINDOWS} />
        </>}
      </Top>
      <div className="bt-kinds">
        <span className="muted">Markets</span>
        <span className="az-seg" role="group">
          {KINDS.map(([k, label]) => <button key={k} className={kinds.includes(k) ? "on" : ""} aria-pressed={kinds.includes(k)} onClick={() => flip(k)}>{label}</button>)}
        </span>
      </div>
      {leagueList.length > 0 && (
        <div className="bt-kinds">
          <span className="muted">Leagues</span>
          <span className="az-seg" role="group" aria-label="Leagues">
            <button className={leagues.length ? "" : "on"} aria-pressed={!leagues.length} onClick={() => setLeagues([])}>All</button>
            {leagueList.map((l) => (
              <button key={l.code} className={leagues.includes(l.code) ? "on" : ""} aria-pressed={leagues.includes(l.code)}
                onClick={() => flipLeague(l.code, l.name)}>{l.name}{l.games ? ` (${l.games})` : ""}</button>
            ))}
          </span>
        </div>
      )}
      {view === "mixer" && <Mixer q={`${q}&book=${book}&tickets=${tickets}`} pub={pub} />}
      {view === "vet" && <Vet code={vetCode} q={`markets=${kinds.join(",")}&leagues=${encodeURIComponent(leagues.join(","))}`} pub={pub} />}
      {view === "slip" && <>
      {!c ? <p className="az-note">Finding the best slip…</p> : onlyScorers ? null : s ? (
        <>
          <div className="az-kpis">
            <div><span className="muted">Total odds</span><b className="num">{odd(s.odds)}</b></div>
            <div><span className="muted">Bookmaker</span><b>{BOOK[s.book] || s.book}</b></div>
            <div><span className="muted">Games</span><b className="num">{s.n}</b></div>
            <div><span className="muted">Least likely game</span><b className="num">{pct(s.least, 1)}</b></div>
            <div><span className="muted">Chance of winning</span><b className="num">{chance(s.p)}</b></div>
            <div><span className="muted">Edge at this price</span><b className={`num ${tone(s.edge)}`}>{sgn(s.edge)}</b></div>
            <div><span className="muted">Last kick-off</span><b className="num">{kick(s.last_ms)}</b></div>
          </div>
          {s.book === "sportybet" ? (s.coded ? <SportyCode q={q} slip={s.legs.map((x) => x.key).join()} pub={pub} />
            : !pub && <div className="bt-code"><span className="muted">A SportyBet code needs the updated collector on your laptop.</span></div>)
            : (c.books || []).includes("sportybet") && <div className="bt-code"><button className="az-go" onClick={() => setBook("sportybet")}>Build it on SportyBet for a booking code</button></div>}
          <div className="az-scroll"><table className="az-table bt-t">
            <thead><tr><th>Kick-off</th><th>Bet</th><th className="r">Odds</th><th className="r">Fair odds</th><th className="r">Edge</th><th className="r">Chance</th></tr></thead>
            <tbody>
              {s.legs.map((x) => (
                <tr key={x.key}>
                  <td className="muted num">{kick(x.t_ms)}</td>
                  <td className="bt-name"><b>{pick(x)}</b>{x.model && <span className="az-flag muted" title={x.sharp_src}>{MODEL[x.model] || `${x.model} model`}</span>}
                    <span className="muted az-cat">{x.home} v {x.away} · {x.league_name || x.league}</span></td>
                  <td className="r num">{odd(x.odds)}</td>
                  <td className="r num muted">{odd((1 - (x.p_void || 0)) / x.p)}</td>
                  <td className={`r num ${tone(x.edge)}`}>{sgn(x.edge)}</td>
                  <td className="r num" title={x.p_void ? `wins ${pct(x.p, 1)}, refunded ${pct(x.p_void, 1)}` : ""}>{pct(x.p + (x.p_void || 0), 1)}</td>
                </tr>
              ))}
            </tbody>
          </table></div>
        </>
      ) : (
        <p className="az-note">
          {!c.bets ? (c.picked?.length ? `No games in ${c.picked.map((code) => leagueList.find((l) => l.code === code)?.name || code).join(", ")} in this window with fresh odds for these markets.`
            : c.book && c.books?.length && !c.books.includes(c.book)
            ? `${BOOK[c.book] || c.book} has none of these markets in this window: ${c.books.map((k) => BOOK[k] || k).join(", ")} ${c.books.length > 1 ? "do" : "does"}.`
            : kinds.length < ALL_KINDS.length && !c.books?.length ? "None of the books has these markets in this window."
            : "No fresh odds from the books for games in this window right now.")
            : c.target > c.reach ? `With up to ${g.games} games, the bets in this window reach ${odd(c.reach)} at most.`
            : `No slip of ${pct(floor)}+ games lands between ${odd(c.target)} and ${odd(c.target * (1 + g.tol))} in this window.`}
        </p>
      )}
      {c && c.bets > 0 && !onlyScorers && (
        <div className="az-scroll"><table className="az-table bt-t az-click">
          <thead><tr><th>Target</th><th className="r">Slip</th><th className="r">Games</th><th className="r">Least likely game</th><th>Book</th><th className="r">Chance of winning</th><th className="r">Edge</th>
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
                  <td className="r num">{r.n ? pct(r.least, 1) : "-"}</td>
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
      {c?.scorers && <Scorers sc={c.scorers} q={q} target={t} pub={pub} />}
      {c && <Models m={c.models} kinds={kinds} />}
      </>}
    </section>
  );
}

// How the goals and corners models did on past games: what they said against what happened, and how much better their
// chances fit the results than each league's plain rate
function Models({ m, kinds }) {
  const rows = [];
  const goals = m?.goals?.markets || {};
  const want = { o15: ["O15"], o25: ["O25"], btts: ["GG", "NG"], mix: ["1|O15", "2|O15", "1|O25", "2|O25"],
    handicap: ["AH1M15", "AH2M15", "AH1P15", "AH2P15", "EH011", "EH101"] };
  for (const [k, codes] of Object.entries(want)) if (kinds.includes(k)) codes.forEach((code) => goals[code]?.n && rows.push([code, goals[code]]));
  for (const [k, stats] of [["corners", ["corners"]], ["shots", ["shots", "sot"]], ["saves", ["saves"]]]) {
    if (kinds.includes(k)) stats.forEach((st) => Object.entries(m?.[st]?.markets || {}).forEach(([code, v]) => v.n && rows.push([code, v])));
  }
  if (!rows.length) return null;
  return (
    <div className="az-scroll"><table className="az-table bt-t">
      <thead><tr><th>Model on past games</th><th className="r">Games</th><th className="r">Said</th><th className="r">Happened</th>
        <th className="r" title="How much better the model's chances fit the results than the league's plain rate (log loss)">Better than the league rate</th></tr></thead>
      <tbody>
        {rows.map(([code, v]) => (
          <tr key={code}>
            <td>{more({ market: code, home: "Home", away: "Away" })}</td><td className="r num muted">{v.n.toLocaleString()}</td>
            <td className="r num">{pct(v.said, 1)}</td><td className="r num">{pct(v.won / v.n, 1)}</td>
            <td className={`r num ${v.ll_base > 0.05 ? tone(v.ll_base - v.ll) : ""}`}>{v.ll_base > 0.05 ? sgn((v.ll_base - v.ll) / v.ll_base) : "-"}</td>
          </tr>
        ))}
      </tbody>
    </table></div>
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
                <td className="muted num">{kick(b.t_ms)}</td><td className="bt-name"><b>{pick(b)}</b> <span className="muted">{BOOK[b.book] || b.book}</span><Rule x={b} /></td>
                <td className="r num">{odd(b.odds)}</td><td className={`r num ${tone(ruled(b).edge)}`}>{sgn(ruled(b).edge)}</td>
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
          <div className="tw-5 term-col"><RollSide d={d} /></div>
          <div className="tw-7"><Value d={d} /></div>
          <Combo stamp={d.now_ms} pub={d.public} />
          {!d.public && <Record rec={d.record} recent={d.flat_recent} pub={d.public} />}
          {d.study && <Study s={d.study} methods={d.methods} method={d.method} />}
          <div className={d.public ? "term-wide" : "tw-6"}><Patterns rows={d.patterns} /></div>
          {!d.public && <div className="tw-6"><Sources s={d.sources} now={now} d={d} /></div>}
        </div>
      )}
    </div>
  );
}

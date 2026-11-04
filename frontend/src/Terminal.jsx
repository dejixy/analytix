import { useEffect, useRef, useState } from "react";
import Edges, { Tape } from "./components/Edges.jsx";
import News from "./components/News.jsx";
import Bot from "./components/Bot.jsx";
import { CommandBar, StatusBar, WorldClocks } from "./components/TermChrome.jsx";
import { soundOn, unlockSound } from "./botSound.js";
import { fmtTime } from "./format.js";

/* The edge terminal (/terminal): the event edges, the news and the bot, on their own page next to the dashboard.
   The tab lives in the address (#edges, #news, #bot) so a link or a reload lands on it. A command line jumps to any
   market or panel; the status bar shows how fresh every source is. */

const TABS = [
  ["edges", "Edges", "commodities · roll · yen"],
  ["news", "News", "calendar · headlines"],
  ["bot", "Bot", "paper trading"],
];
const fromHash = () => {
  const h = window.location.hash.replace("#", "");
  return TABS.some(([k]) => k === h) ? h : "edges";
};
const EDGES_EVERY_MS = 5000;

export default function Terminal() {
  const [tab, setTab] = useState(fromHash);
  const [d, setD] = useState(null);
  const [error, setError] = useState(null);
  const [now, setNow] = useState(Date.now());
  const [latency, setLatency] = useState(null);
  const [lastFetch, setLastFetch] = useState(null);
  const pending = useRef(null);                            // a jump waiting for the Edges tab to render

  const go = (k) => {
    window.history.replaceState(null, "", k === "edges" ? "/terminal" : `/terminal#${k}`);
    setTab(k);
  };

  /** Scroll to a panel (and a market's row in it) and light it up for a moment. */
  const jump = (panel, mk) => {
    const el = document.getElementById(`p-${panel}`);
    if (!el) return;
    el.scrollIntoView({ behavior: "smooth", block: "start" });
    const row = mk ? el.querySelector(`[data-mk="${mk}"]`) : null;
    for (const x of [el, row].filter(Boolean)) {
      x.classList.remove("term-hl");
      void x.offsetWidth;                                 // restart the highlight
      x.classList.add("term-hl");
      setTimeout(() => x.classList.remove("term-hl"), 1800);
    }
  };

  const run = (c) => {
    if (c.href) {
      window.location.href = c.href;
    } else if (c.tab) {
      go(c.tab);
    } else if (c.panel) {
      if (c.view) setTimeout(() => window.dispatchEvent(new CustomEvent("term-view", { detail: c })), 0);
      if (tab !== "edges") {
        pending.current = [c.panel, c.mk];
        go("edges");
      } else jump(c.panel, c.mk);
    }
  };

  useEffect(() => {
    if (tab === "edges" && d && pending.current) {
      const [p, mk] = pending.current;
      pending.current = null;
      setTimeout(() => jump(p, mk), 60);
    }
  }, [tab, d]);

  useEffect(() => {
    const onHash = () => setTab(fromHash());
    window.addEventListener("hashchange", onHash);
    const t = setInterval(() => setNow(Date.now()), 1000);
    const arm = () => soundOn() && unlockSound();         // the bot's chime needs a click first
    document.addEventListener("pointerdown", arm, { once: true });
    document.title = "Analytix · Terminal";
    const onKey = (e) => {                                 // 1, 2, 3 switch tabs (not while typing)
      if (e.metaKey || e.ctrlKey || e.altKey || /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName)) return;
      const k = TABS[Number(e.key) - 1]?.[0];
      if (k) go(k);
    };
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("hashchange", onHash);
      window.removeEventListener("keydown", onKey);
      clearInterval(t);
      document.removeEventListener("pointerdown", arm);
    };
  }, []);

  // One request at a time: the next waits for this reply (a slow link can't pile them up). The slow parts (weekend
  // record, hourly history) only come when they've changed: the page says which version it holds.
  const heavy = useRef(null);
  useEffect(() => {
    let live = true;
    let timer;
    const every = tab === "edges" ? EDGES_EVERY_MS : 60000;
    const load = async () => {
      const t0 = performance.now();
      try {
        const have = heavy.current?.heavy_v;
        const r = await fetch(`/api/edges${have ? `?have=${have}` : ""}`);
        const j = await r.json();
        if (!live) return;
        const ms = performance.now() - t0;
        setLatency(ms);
        setLastFetch(Date.now());
        if (r.ok) {
          if (j.light && heavy.current) Object.assign(j, { weekend: heavy.current.weekend, compare: heavy.current.compare, activity: heavy.current.activity });
          else heavy.current = { heavy_v: j.heavy_v, weekend: j.weekend, compare: j.compare, activity: j.activity };
          setD(j);
          setError(null);
        } else setError(j.detail || "Couldn't read the edges.");
        timer = setTimeout(load, Math.max(every - ms, 1000));
      } catch {
        if (!live) return;
        setError("Couldn't reach the server.");
        timer = setTimeout(load, every);
      }
    };
    load();
    return () => {
      live = false;
      clearTimeout(timer);
    };
  }, [tab]);

  const cme = d?.markets?.find((m) => m.ticker === "CL")?.session?.open;
  return (
    <div className="app term">
      <header className="topbar">
        <div className="brand">
          <span className="brand-name">ANALYTIX<span className="cursor" aria-hidden="true" /></span>
          <span className="brand-tag">edge terminal</span>
        </div>
        <nav className="coins" aria-label="Section">
          {TABS.map(([k, name, meta], i) => (
            <button key={k} className={`coin-tab ${tab === k ? "active" : ""}`} aria-pressed={tab === k} onClick={() => go(k)}
              title={`${name} (key ${i + 1})`}>
              <span className="coin-name"><span className="term-fkey">{i + 1}</span>{name}</span>
              <span className="coin-meta">{meta}</span>
            </button>
          ))}
        </nav>
        <div className="header-right">
          <a className="alerts-open term-link" href="/">‹ Dashboard</a>
          <span className="badge" title={error || ""}>
            <span className={`dot ${error ? "critical" : d?.mode === "replay" ? "info" : "good pulse"}`} />
            {error ? "Server offline" : d?.mode === "replay" ? "Replay" : "Live"}
          </span>
          <span className="clock num" title="Your local time">{fmtTime(now)}</span>
        </div>
      </header>
      <div className="term-bar">
        <CommandBar onRun={run} />
        <WorldClocks now={now} cme={cme} />
      </div>
      <div className="term-tape-wrap">
        <Tape d={d} />
        {tab === "edges" && lastFetch && <i key={lastFetch} className="term-sweep" style={{ animationDuration: `${EDGES_EVERY_MS}ms` }} title="Next refresh" />}
      </div>
      {tab === "edges" && <Edges d={d} error={error} now={now} onPick={(m) => jump(m.kind === "fx" ? "FX" : "CMDY", m.ticker)} />}
      {tab === "news" && <News inline coins={d?.coins} />}
      {tab === "bot" && <Bot inline />}
      <StatusBar d={d} error={error} now={now} latency={latency} lastFetch={lastFetch} every={tab === "edges" ? EDGES_EVERY_MS : 60000} />
    </div>
  );
}

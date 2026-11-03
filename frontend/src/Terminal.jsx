import { useEffect, useState } from "react";
import Edges, { Tape } from "./components/Edges.jsx";
import News from "./components/News.jsx";
import Bot from "./components/Bot.jsx";
import { soundOn, unlockSound } from "./botSound.js";
import { fmtTime } from "./format.js";

/* The edge terminal (/terminal): the event edges, the news and the bot, on their own page next to the dashboard.
   The tab lives in the address (#edges, #news, #bot) so a link or a reload lands on it. */

const TABS = [
  ["edges", "Edges", "commodities · roll · yen"],
  ["news", "News", "calendar · headlines"],
  ["bot", "Bot", "paper trading"],
];
const fromHash = () => {
  const h = window.location.hash.replace("#", "");
  return TABS.some(([k]) => k === h) ? h : "edges";
};

export default function Terminal() {
  const [tab, setTab] = useState(fromHash);
  const [d, setD] = useState(null);
  const [error, setError] = useState(null);
  const [now, setNow] = useState(Date.now());
  const go = (k) => {
    window.history.replaceState(null, "", k === "edges" ? "/terminal" : `/terminal#${k}`);
    setTab(k);
  };

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

  useEffect(() => {
    let live = true;
    const load = async () => {
      try {
        const r = await fetch("/api/edges");
        const j = await r.json();
        if (!live) return;
        if (r.ok) {
          setD(j);
          setError(null);
        } else setError(j.detail || "Couldn't read the edges.");
      } catch {
        if (live) setError("Couldn't reach the server.");
      }
    };
    load();
    const t = setInterval(load, tab === "edges" ? 5000 : 60000);
    return () => {
      live = false;
      clearInterval(t);
    };
  }, [tab]);


  return (
    <div className="app term">
      <header className="topbar">
        <div className="brand">
          <span className="brand-name">ANALYTIX<span className="cursor" aria-hidden="true" /></span>
          <span className="brand-tag">edge terminal</span>
        </div>
        <nav className="coins" aria-label="Section">
          {TABS.map(([k, name, meta]) => (
            <button key={k} className={`coin-tab ${tab === k ? "active" : ""}`} aria-pressed={tab === k} onClick={() => go(k)}>
              <span className="coin-name">{name}</span>
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
      <Tape d={d} />
      {tab === "edges" && <Edges d={d} error={error} now={now} />}
      {tab === "news" && <News inline coins={d?.coins} />}
      {tab === "bot" && <Bot inline />}
      <footer className="term-foot muted">
        <span>keys <b>1</b> <b>2</b> <b>3</b> switch tabs</span>
        <span>{d ? `prices ${Math.max(0, Math.round((now - d.now_ms) / 1000))}s old · ` : ""}times in your time zone</span>
      </footer>
    </div>
  );
}

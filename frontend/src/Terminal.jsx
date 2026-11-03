import { useEffect, useState } from "react";
import Edges from "./components/Edges.jsx";
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

  useEffect(() => {
    const onHash = () => setTab(fromHash());
    window.addEventListener("hashchange", onHash);
    const t = setInterval(() => setNow(Date.now()), 1000);
    const arm = () => soundOn() && unlockSound();         // the bot's chime needs a click first
    document.addEventListener("pointerdown", arm, { once: true });
    document.title = "Analytix · Terminal";
    return () => {
      window.removeEventListener("hashchange", onHash);
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

  const go = (k) => {
    window.history.replaceState(null, "", k === "edges" ? "/terminal" : `/terminal#${k}`);
    setTab(k);
  };

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
      {tab === "edges" && <Edges d={d} error={error} />}
      {tab === "news" && <News inline coins={d?.coins} />}
      {tab === "bot" && <Bot inline />}
    </div>
  );
}

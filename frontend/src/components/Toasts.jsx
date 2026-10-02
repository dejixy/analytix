import { useEffect, useRef, useState } from "react";
import { arrow } from "../format.js";

const SHOW_MS = 9000;

/** Pops up new broken levels and cascades for the coin on screen, once each (later updates show in the log).
 *  Whatever is already in the feed when a coin is opened — or reopened, or a replay restarts — is treated as
 *  seen, so switching coins never replays old news. */
export default function Toasts({ coin, nowMs, marketEvents }) {
  const seen = useRef({ coin: null, ids: new Set(), now: 0 });
  const [toasts, setToasts] = useState([]);

  useEffect(() => {
    if (!coin) return;
    const s = seen.current;
    if (s.coin !== coin || nowMs < s.now) {
      // new coin on screen, or the clock went backwards (a replay restarted): start from what's there now
      seen.current = { coin, ids: new Set(marketEvents.map((e) => e.id)), now: nowMs };
      return;
    }
    s.now = nowMs;
    const fresh = marketEvents.filter((e) => !s.ids.has(e.id));
    if (!fresh.length) return;
    fresh.forEach((e) => s.ids.add(e.id));
    const until = Date.now() + SHOW_MS;
    setToasts((t) => [...fresh.map((e) => ({ ...e, coin, until, key: `${e.id}|${until}` })), ...t].slice(0, 3));
  }, [coin, nowMs, marketEvents]);

  useEffect(() => {
    if (!toasts.length) return undefined;
    const timer = setInterval(() => setToasts((t) => t.filter((x) => x.until > Date.now())), 1000);
    return () => clearInterval(timer);
  }, [toasts.length]);

  if (!toasts.length) return null;
  return (
    <div className="toasts" role="status" aria-live="polite">
      {toasts.map((t) => (
        <div key={t.key} className={`toast ${t.direction}`}>
          <div className="toast-head">
            <span className="chip">{t.kind === "cascade" ? "CASCADE" : "LEVEL"}</span>
            <span className="muted">{t.coin}{t.window ? ` · ${t.window}` : ""}</span>
            <button className="toast-x" aria-label="Dismiss" onClick={() => setToasts((all) => all.filter((x) => x.key !== t.key))}>×</button>
          </div>
          <div className="toast-title">{arrow(t.direction)} {t.title}</div>
          {t.detail && <div className="toast-detail muted">{t.detail}</div>}
        </div>
      ))}
    </div>
  );
}

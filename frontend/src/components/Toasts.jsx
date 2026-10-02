import { useEffect, useRef, useState } from "react";
import { arrow } from "../format.js";

const SHOW_MS = 9000;

/** Pops up new broken levels and cascades for the coin on screen, once each (later updates show in the log). Events already in the feed when a coin
 *  is first opened are treated as seen, so switching coins doesn't replay old news. */
export default function Toasts({ coin, marketEvents }) {
  const seen = useRef({});
  const [toasts, setToasts] = useState([]);

  useEffect(() => {
    if (!coin) return;
    const known = seen.current[coin];
    if (!known) {
      seen.current[coin] = new Set(marketEvents.map((e) => e.id));
      return;
    }
    const fresh = marketEvents.filter((e) => !known.has(e.id));
    if (!fresh.length) return;
    fresh.forEach((e) => known.add(e.id));
    setToasts((t) => [...fresh.map((e) => ({ ...e, key: `${e.id}|${Date.now()}` })), ...t].slice(0, 3));
  }, [coin, marketEvents]);

  useEffect(() => {
    if (!toasts.length) return undefined;
    const timer = setTimeout(() => setToasts((t) => t.slice(0, -1)), SHOW_MS);
    return () => clearTimeout(timer);
  }, [toasts]);

  if (!toasts.length) return null;
  return (
    <div className="toasts" role="status" aria-live="polite">
      {toasts.map((t) => (
        <div key={t.key} className={`toast ${t.direction}`}>
          <div className="toast-head">
            <span className="chip">{t.kind === "cascade" ? "CASCADE" : "LEVEL"}</span>
            <span className="muted">{coin}{t.window ? ` · ${t.window}` : ""}</span>
            <button className="toast-x" aria-label="Dismiss" onClick={() => setToasts((all) => all.filter((x) => x.key !== t.key))}>×</button>
          </div>
          <div className="toast-title">{arrow(t.direction)} {t.title}</div>
          {t.detail && <div className="toast-detail muted">{t.detail}</div>}
        </div>
      ))}
    </div>
  );
}

import { useEffect, useRef, useState } from "react";
import { chime } from "../botSound.js";
import { notifyEnabled } from "./AlertsPanel.jsx";

/* The bot's live alert, on top of every view: which coin and side, a countdown to get in, then one to close.
   Each new alert chimes (when sound is on) and shows a desktop notification while the tab is in the background. */

const usdk = (x) => (x >= 1000 ? `$${(x / 1000).toFixed(1)}k` : `$${Math.round(x)}`);

export default function BotBanner({ bot, nowMs, onOpen }) {
  const a = bot?.alert;
  const seen = useRef(new Set());
  const [hidden, setHidden] = useState(null);

  useEffect(() => {
    if (!a || seen.current.has(a.id)) return;
    seen.current.add(a.id);
    chime(a.side);
    if (document.hidden && notifyEnabled() && "Notification" in window && Notification.permission === "granted") {
      try {
        new Notification(`${a.coin} ${a.side > 0 ? "LONG" : "SHORT"} now`, {
          body: `Big mover ${a.side > 0 ? "bought" : "sold"} ${usdk(a.usd)}. Get in within ${a.fill_s}s, close after ${a.hold_s}s.`,
          tag: a.id,
        });
      } catch {
        /* some browsers only allow notifications from a service worker */
      }
    }
  }, [a?.id]);

  if (!a || hidden === a.id || !nowMs) return null;
  const age = Math.max(0, (nowMs - a.t_ms) / 1000);
  const inLeft = Math.ceil(a.fill_s - age);
  const closeLeft = Math.ceil(a.fill_s + a.hold_s - age);
  if (closeLeft <= 0) return null;
  const long = a.side > 0;
  return (
    <div className={`bm-banner ${long ? "long" : "short"}`} role="alert" onClick={onOpen}>
      <b className="bm-banner-call">{a.coin} {long ? "LONG" : "SHORT"}</b>
      <span className="bm-banner-time">{inLeft > 0 ? `get in now · ${inLeft}s` : `close in ${closeLeft}s`}</span>
      <span className="bm-banner-why">
        {a.wallets > 1 ? `${a.wallets} big movers` : "Big mover"} {long ? "bought" : "sold"} {usdk(a.usd)}
      </span>
      <button className="plan-close" onClick={(e) => { e.stopPropagation(); setHidden(a.id); }} aria-label="Hide">✕</button>
    </div>
  );
}

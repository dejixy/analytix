import { useEffect, useRef, useState } from "react";
import { chime } from "../botSound.js";
import { notifyEnabled } from "./AlertsPanel.jsx";

/* The bot's live call, on top of every view: a big TWAP was spotted. A countdown to get in, then one to close (or,
   holding until the TWAP stops, "close now" when it does). The call and the stop each chime (when sound is on) and
   show a desktop notification while the tab is in the background. */

const usdk = (x) => (x >= 1000 ? `$${(x / 1000).toFixed(1)}k` : `$${Math.round(x)}`);
const mins = (s) => (s < 60 ? `${Math.max(0, Math.round(s))}s` : `${Math.floor(s / 60)}m ${String(Math.round(s % 60)).padStart(2, "0")}s`);

function notify(title, body, tag) {
  if (document.hidden && notifyEnabled() && "Notification" in window && Notification.permission === "granted") {
    try {
      new Notification(title, { body, tag });
    } catch {
      /* some browsers only allow notifications from a service worker */
    }
  }
}

export default function BotBanner({ bot, nowMs, onOpen }) {
  const a = bot?.status === "live" ? bot?.alert : null;
  const seen = useRef(new Set());
  const [hidden, setHidden] = useState(null);
  const stopped = a && a.hold_s == null && !a.running;

  useEffect(() => {
    if (!a) return;
    const side = a.side > 0 ? "LONG" : "SHORT";
    if (!seen.current.has(a.id)) {
      seen.current.add(a.id);
      chime(a.side);
      notify(`${a.coin} ${side} now`, `Big TWAP ${a.side > 0 ? "buying" : "selling"} ${usdk(a.slice_usd)} every 30s. Get in within ${a.fill_s}s.`, a.id);
    }
    if (stopped && !seen.current.has(`${a.id}-end`)) {
      seen.current.add(`${a.id}-end`);
      chime(-a.side);
      notify(`${a.coin} TWAP stopped`, `Close your ${side.toLowerCase()} now.`, `${a.id}-end`);
    }
  }, [a?.id, stopped]);

  if (!a || hidden === a.id || !nowMs) return null;
  const age = Math.max(0, (nowMs - a.t_ms) / 1000);
  const inLeft = Math.ceil(a.fill_s - age);
  let when;
  if (inLeft > 0) when = `get in now · ${inLeft}s`;
  else if (a.hold_s != null) {
    const left = a.fill_s + a.hold_s - age;
    if (left <= 0) return null;
    when = `close in ${mins(left)}`;
  } else when = a.running ? `hold · running ${mins(age)}` : "stopped · close now";
  const long = a.side > 0;
  return (
    <div className={`bm-banner ${long ? "long" : "short"}`} role="alert" onClick={onOpen}>
      <b className="bm-banner-call">{a.coin} {long ? "LONG" : "SHORT"}</b>
      <span className="bm-banner-time">{when}</span>
      <span className="bm-banner-why">
        Big TWAP {long ? "buying" : "selling"} {usdk(a.slice_usd)} every 30s{a.plan ? ` · ${usdk(a.plan.left_usd)} to come` : ""}
      </span>
      <button className="plan-close" onClick={(e) => { e.stopPropagation(); setHidden(a.id); }} aria-label="Hide">✕</button>
    </div>
  );
}

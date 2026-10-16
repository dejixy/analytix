/* The bot's alert chime. Browsers only allow sound after a click on the page, so turning sound on (a click)
   unlocks it, and App resumes it on the first click after a reload. Rising for a long, falling for a short. */
const KEY = "analytix.botSound";
let ctx = null;

export function soundOn() {
  try {
    return localStorage.getItem(KEY) === "1";
  } catch {
    return false;
  }
}

export function unlockSound() {
  try {
    ctx = ctx || new (window.AudioContext || window.webkitAudioContext)();
    if (ctx.state === "suspended") ctx.resume();
  } catch {
    ctx = null;
  }
}

export function setSound(on) {
  try {
    localStorage.setItem(KEY, on ? "1" : "0");
  } catch {
    /* private window: sound just won't be remembered */
  }
  if (on) {
    unlockSound();
    chime(1, true);
  }
}

export function chime(side, force = false) {
  if (!force && !soundOn()) return;
  unlockSound();
  if (!ctx) return;
  const notes = side > 0 ? [660, 990] : [990, 660];
  notes.forEach((f, i) => {
    const o = ctx.createOscillator();
    const g = ctx.createGain();
    const t = ctx.currentTime + i * 0.15;
    o.type = "sine";
    o.frequency.value = f;
    g.gain.setValueAtTime(0.0001, t);
    g.gain.exponentialRampToValueAtTime(0.3, t + 0.02);
    g.gain.exponentialRampToValueAtTime(0.0001, t + 0.14);
    o.connect(g).connect(ctx.destination);
    o.start(t);
    o.stop(t + 0.15);
  });
}

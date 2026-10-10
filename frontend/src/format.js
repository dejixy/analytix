export function fmtPrice(p) {
  if (p == null) return "-";
  const d = p >= 1 ? 2 : p >= 0.01 ? 4 : 6;
  return p.toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });
}

export const fmtPct = (x, digits = 2) => (x == null ? "-" : `${x >= 0 ? "+" : "−"}${Math.abs(x).toFixed(digits)}%`);

export const fmtSigned = (x, digits = 2) => (x == null ? "-" : `${x >= 0 ? "+" : "−"}${Math.abs(x).toFixed(digits)}`);

export function fmtUsd(x) {
  if (x == null) return "-";
  const a = Math.abs(x);
  const s = a >= 1e9 ? `$${(a / 1e9).toFixed(2)}B` : a >= 1e6 ? `$${(a / 1e6).toFixed(2)}M`
    : a >= 1e3 ? `$${(a / 1e3).toFixed(0)}K` : `$${a.toFixed(0)}`;
  return x < 0 ? `−${s}` : s;
}

export const fmtSize = (x) => (x == null ? "-" : x >= 100 ? x.toFixed(1) : x >= 1 ? x.toFixed(3) : x.toFixed(4));

export const fmtTime = (ms) =>
  ms ? new Date(ms).toLocaleTimeString(undefined, { hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "-";

export const arrow = (dir) => (dir === "up" ? "▲" : dir === "down" ? "▼" : "•");

/** "ETH +0.37% in 1m: driven by X" → "Driven by X" (the card shows the move separately). */
export function headlineBody(headline) {
  const m = headline.match(/^\S+ (?:[+−-]?[\d.]+% in \S+|little changed over \S+ \([^)]*\)): /);
  const body = m ? headline.slice(m[0].length) : headline;
  return body.charAt(0).toUpperCase() + body.slice(1);
}

/** How far price moved for the money traded, for the card footer: "moved 0.3× usual · price held". */
export function impactText(im) {
  if (!im) return null;
  if (im.verdict === "against") return "price went the other way";
  const r = `moved ${im.ratio.toFixed(1)}× usual`;
  return im.verdict === "absorbed" ? `${r} · price held` : im.verdict === "outsized" ? `${r} · big reaction` : r;
}

export function impactHelp(im, window) {
  if (!im) return "";
  const side = im.net_flow >= 0 ? "buying" : "selling";
  return (
    `Net ${side} of ${fmtUsd(Math.abs(im.net_flow))} usually moves price ${fmtPct(im.expected_bps / 100)} over ${window}. ` +
    `This time it moved ${fmtPct(im.actual_bps / 100)}. Under 0.35× usual means orders waiting in the book soaked it up. ` +
    `Over 2.5× means the book was thin, or other exchanges led the move. ` +
    `Here, $1M of net ${side} usually moves price about ${(im.lam_bps_per_m / 100).toFixed(3)}%.`
  );
}

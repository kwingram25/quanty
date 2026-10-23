export const money = (v: number | null | undefined, signed = false): string => {
  const x = v ?? 0;
  const sign = x < -0.005 ? "−" : signed && x > 0.005 ? "+" : "";
  return `${sign}$${Math.abs(x).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
};

export const tone = (v: number | null | undefined): "up" | "down" | "" =>
  (v ?? 0) > 0.005 ? "up" : (v ?? 0) < -0.005 ? "down" : "";

export const cents = (p: number | null | undefined): string => {
  if (p == null) return "–";
  const c = Math.round(p * 1000) / 10; // tenths of a cent, without float noise
  return `${Number.isInteger(c) ? c.toFixed(0) : c.toFixed(1)}¢`;
};

const pad = (n: number) => String(n).padStart(2, "0");

export const clock = (ms: number, seconds = true): string => {
  const d = new Date(ms);
  const hm = `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
  return seconds ? `${hm}:${pad(d.getUTCSeconds())}` : hm;
};

export const ago = (ms: number | null): string => {
  if (!ms) return "never";
  const s = Math.max(0, Date.now() / 1000 - ms / 1000);
  if (s < 90) return `${Math.round(s)} s ago`;
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 172800) return `${Math.round(s / 3600)} h ago`;
  return new Date(ms).toISOString().slice(0, 10);
};

/** "synthetic:UPDOWN-1792737300" → the part after the venue. */
export const marketName = (id: string): string => id.split(":").slice(1).join(":");

export const seriesName = (series: string): string => series.split(":").slice(1).join(":").replace(/_/g, " ");

export const duration = (s: number): string => {
  if (s <= 0) return "closed";
  if (s < 60) return `${Math.ceil(s)}s`;
  const m = Math.floor(s / 60);
  return m < 60 ? `${m}:${pad(Math.floor(s % 60))}` : `${Math.floor(m / 60)}h ${pad(m % 60)}m`;
};

/** Stable color slot per strategy name: sorted order, max 8 (the palette's validated slots). */
export const slots = (names: string[]): Record<string, number> =>
  Object.fromEntries([...names].sort().map((n, i) => [n, Math.min(i, 7) + 1]));

import { useSyncExternalStore } from "react";

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

/** Which time zone the dashboard shows times in: the viewer's own (default) or UTC. Remembered per
 * browser; components re-render on a change through useZone (the app root subscribes). */
export type Zone = "local" | "utc";
let zone: Zone = (() => {
  try { return localStorage.getItem("zone") === "utc" ? "utc" : "local"; } catch { return "local"; }
})();
const listeners = new Set<() => void>();
const formats = new Map<string, Intl.DateTimeFormat>();

export const setZone = (z: Zone) => {
  zone = z;
  formats.clear();
  try { localStorage.setItem("zone", z); } catch { /* private mode */ }
  listeners.forEach((f) => f());
};
export const useZone = (): Zone =>
  useSyncExternalStore((f) => { listeners.add(f); return () => { listeners.delete(f); }; }, () => zone);

const fmt = (key: string, opts: Intl.DateTimeFormatOptions): Intl.DateTimeFormat => {
  let f = formats.get(key);
  if (!f) {
    // UTC reads as 24-hour, as it's usually written; local time follows the browser's locale
    f = new Intl.DateTimeFormat(undefined, zone === "utc" ? { ...opts, timeZone: "UTC", hourCycle: "h23" } : opts);
    formats.set(key, f);
  }
  return f;
};

/** "PDT", "UTC": the zone times are shown in (at that moment, for daylight saving). */
export const zoneName = (ms = Date.now(), long = false): string =>
  fmt(long ? "zl" : "zs", { timeZoneName: long ? "long" : "short" })
    .formatToParts(ms).find((p) => p.type === "timeZoneName")?.value ?? (zone === "utc" ? "UTC" : "");

export const clock = (ms: number, seconds = true): string =>
  fmt(seconds ? "hms" : "hm", { hour: "numeric", minute: "2-digit", ...(seconds ? { second: "2-digit" } : {}) }).format(ms);

/** "Thu" in the shown zone. */
export const weekday = (ms: number): string => fmt("wd", { weekday: "short" }).format(ms);

/** A time in a long history: "3:29:27 PM" on the reference day (the run's now), "Thu 3:29 PM" within
 *  the week before it, "Oct 14, 3:29 PM" further back. */
export const when = (ms: number, ref: number): string => {
  const day = fmt("ymd", { year: "numeric", month: "2-digit", day: "2-digit" });
  if (day.format(ms) === day.format(ref)) return clock(ms);
  if (ref - ms < 6.5 * 86_400_000) return `${weekday(ms)} ${clock(ms, false)}`;
  return `${fmt("md", { month: "short", day: "numeric" }).format(ms)}, ${clock(ms, false)}`;
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
  if (m < 60) return `${m}:${pad(Math.floor(s % 60))}`;
  const h = Math.floor(m / 60);
  return h < 24 ? `${h}h ${pad(m % 60)}m` : `${Math.floor(h / 24)}d ${pad(h % 24)}h`;
};

/** "Thu 2:00 PM" for closes more than a day away, "2:00 PM" otherwise (in the shown zone). */
export const closesAt = (ms: number, now: number | null): string => {
  const hm = clock(ms, false);
  return now != null && ms - now > 86_400_000 ? `${weekday(ms)} ${hm}` : hm;
};

/** A market's display name, from the snapshot's names (falls back to the id). */
export const nameOf = (names: Record<string, { title: string; subtitle: string }> | undefined, id: string) => {
  const n = names?.[id];
  return n ? { title: n.title, subtitle: n.subtitle } : { title: marketName(id), subtitle: "" };
};

/** Stable color slot per strategy name: sorted order, max 8 (the palette's validated slots). */
export const slots = (names: string[], kinds?: Record<string, string>): Record<string, number> => {
  // real-money strategies first, then the rest, each by name; past the 8th, gray (slot 0) rather than
  // a repeated color. Stable while the set of strategies is.
  const live = (n: string) => (kinds?.[n] === "live" ? 0 : 1);
  const order = [...names].sort((a, b) => live(a) - live(b) || (a < b ? -1 : a > b ? 1 : 0));
  return Object.fromEntries(order.map((n, i) => [n, i < 8 ? i + 1 : 0]));
};

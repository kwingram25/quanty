import { AnimatePresence, motion } from "motion/react";
import { useEffect, useRef, useState } from "react";
import type { OpenMarket, SessionState } from "../api";
import { cents, closesAt, duration, money, seriesName } from "../format";

/** The session's event clock, extrapolated between heartbeats at the rate it has been advancing
 *  (1x live, faster in a sped-up demo). */
function useEventClock(s: SessionState | null): number | null {
  const prev = useRef<{ hb: number; now: number } | null>(null);
  const rate = useRef(1);
  const [, tick] = useState(0);
  useEffect(() => {
    if (!s?.now) return;
    const p = prev.current;
    if (p && s.heartbeat_ts > p.hb && s.now >= p.now) {
      const r = (s.now - p.now) / (s.heartbeat_ts - p.hb);
      rate.current = 0.7 * rate.current + 0.3 * Math.min(Math.max(r, 0), 1000);
    }
    prev.current = { hb: s.heartbeat_ts, now: s.now };
  }, [s?.heartbeat_ts, s?.now]);
  useEffect(() => {
    const id = setInterval(() => tick((n) => n + 1), 250);
    return () => clearInterval(id);
  }, []);
  if (!s?.now) return null;
  return s.now + (Date.now() - s.heartbeat_ts) * rate.current;
}

/** Two numbers that would print the same get enough decimals to tell them apart. */
const close = (a: number, b: number): [string, string] => {
  const big = Math.max(Math.abs(a), Math.abs(b)) >= 1000;
  const fmt = (v: number, d: number) => v.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
  for (const d of big ? [0, 1, 2, 4] : [2, 3, 4, 6]) {
    if (fmt(a, d) !== fmt(b, d)) return [fmt(a, d), fmt(b, d)];
  }
  return [fmt(a, big ? 4 : 6), fmt(b, big ? 4 : 6)];
};

const fmtNum = (v: number) => (Math.abs(v) >= 1000 ? v.toLocaleString("en-US", { maximumFractionDigits: 0 }) : v.toFixed(2));

/** Open markets grouped into events: an up/down window is an event of one market; a range ladder
 *  (or any multi-outcome question) is one event with a row per outcome. */
export function Markets({ session, slots }: { session: SessionState | null; slots: Record<string, number> }) {
  const now = useEventClock(session);
  if (!session) return null;
  const events = new Map<string, OpenMarket[]>();
  for (const m of session.markets) events.set(m.event, [...(events.get(m.event) ?? []), m]);
  return (
    <section className="panel" aria-label="Open markets">
      <header><h2>Open markets</h2><span className="hint">soonest close first</span></header>
      <div className="panel-body">
        {events.size === 0 ? <p className="empty">No markets open right now.</p> : (
          <div className="board">
            <AnimatePresence initial={false}>
              {[...events.entries()].map(([ev, ms]) => (
                <Event key={ev} ms={ms} now={now} slots={slots} exposure={session.exposure?.[ev]} />
              ))}
            </AnimatePresence>
          </div>
        )}
      </div>
    </section>
  );
}

function Event({ ms, now, slots, exposure }: {
  ms: OpenMarket[]; now: number | null; slots: Record<string, number>;
  exposure?: Record<string, { loss: number; gain: number }>;
}) {
  const m0 = ms[0];
  const left = now == null ? (m0.close_ts - m0.open_ts) / 1000 : (m0.close_ts - now) / 1000;
  const frac = Math.min(1, Math.max(0, left / ((m0.close_ts - m0.open_ts) / 1000)));
  const closing = frac < 0.15;
  const ladder = ms.length > 1;
  const ref = m0.reference_value;
  const above = !ladder && ref != null && m0.strike != null ? ref >= m0.strike : null;
  return (
    <motion.div className={`market ${closing ? "closing" : ""}`} layout
      initial={{ opacity: 0, x: 24 }} animate={{ opacity: 1, x: 0 }} exit={{ opacity: 0, x: -24, transition: { duration: 0.2 } }}
      transition={{ type: "spring", stiffness: 380, damping: 32 }}>
      <div className="head">
        <div className="title" title={m0.id}>{m0.title}</div>
        <span className="clock">{duration(left)}</span>
      </div>
      <div className="meta">
        {seriesName(m0.series)} · closes {closesAt(m0.close_ts, now)}
        {ladder && ref != null && <> · now <b className="num">{fmtNum(ref)}{m0.unit}</b></>}
      </div>
      {!ladder && above != null && (
        <div className={`vs ${above ? "up" : "down"}`}>
          {above ? "▲" : "▼"} <b className="num">{close(ref!, m0.strike!)[0]}</b>
          <span className="muted"> {above ? "above" : "below"} {close(ref!, m0.strike!)[1]}</span>
        </div>
      )}
      <div className="outcomes">
        {ms.map((m) => <Outcome key={m.id} m={m} label={ladder ? m.subtitle || m.id : null} slots={slots} />)}
      </div>
      {exposure && Object.entries(exposure).map(([iid, x]) => (
        <div key={iid} className="exposure" title="Across this question's outcomes: the most that could still be lost, and the most it could make">
          <i className="swatch" style={{ ["--c" as string]: `var(--series-${slots[iid] ?? 1})` }} />
          {iid}: risk <span className="down num">{money(x.loss)}</span>, could make <span className="up num">{money(x.gain)}</span>
        </div>
      ))}
      <div className="drain" style={{ width: `${frac * 100}%` }} />
    </motion.div>
  );
}

const price = (p: number | null): number | null => (p != null && p > 0 && p < 1 ? p : null);

/** One outcome as the venues' apps show it: its chance, then what Yes and No cost to buy now. */
function Outcome({ m, label, slots }: { m: OpenMarket; label: string | null; slots: Record<string, number> }) {
  const bid = price(m.bid), ask = price(m.ask);
  const no = bid == null ? null : 1 - bid; // buying No = selling Yes at the best bid
  const chance = bid != null && ask != null ? (bid + ask) / 2 : (bid ?? ask);
  const pct = chance == null ? "–" : `${Math.round(chance * 100)}%`;
  return (
    <div className={`outcome ${label == null ? "single" : ""}`}>
      {label != null ? (
        <><span className="label">{label}</span><span className="chance num">{pct}</span></>
      ) : (
        <span className="label"><b className="num">{pct}</b> <span className="muted">chance</span></span>
      )}
      <span className="buy yes" title="Price to buy Yes">Yes <b className="num">{cents(ask)}</b></span>
      <span className="buy no" title="Price to buy No">No <b className="num">{cents(no)}</b></span>
      <Holders m={m} slots={slots} />
    </div>
  );
}

/** Who holds this outcome, with identical holdings on one line. */
function Holders({ m, slots }: { m: OpenMarket; slots: Record<string, number> }) {
  if (!m.held.length) return null;
  const groups = new Map<string, string[]>();
  for (const h of m.held) {
    const key = `${h.qty} ${h.side === "yes" ? "Yes" : "No"} at ${cents(h.avg)}`;
    groups.set(key, [...(groups.get(key) ?? []), h.instance]);
  }
  return (
    <div className="held">
      {[...groups.entries()].map(([what, who]) => (
        <span key={what} className="mine">
          {who.map((w) => <i key={w} className="swatch" style={{ ["--c" as string]: `var(--series-${slots[w] ?? 1})` }} />)}
          {who.join(", ")} {who.length > 1 ? "hold" : "holds"} {what.replace(" at ", who.length > 1 ? " each at " : " at ")}
        </span>
      ))}
    </div>
  );
}

import { AnimatePresence, motion } from "motion/react";
import { useEffect, useRef, useState } from "react";
import type { OpenMarket, SessionState } from "../api";
import { cents, clock, duration, marketName, seriesName } from "../format";

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

export function Markets({ session, slots }: { session: SessionState | null; slots: Record<string, number> }) {
  const now = useEventClock(session);
  const markets = session?.markets ?? [];
  if (!session) return null;
  return (
    <section className="panel" aria-label="Open markets">
      <header><h2>Open markets</h2><span className="hint">counting down to close; bars drain as time runs out</span></header>
      <div className="panel-body">
        {markets.length === 0 ? <p className="empty">No markets open right now.</p> : (
          <div className="board">
            <AnimatePresence initial={false}>
              {markets.map((m) => <Market key={m.id} m={m} now={now} slots={slots} />)}
            </AnimatePresence>
          </div>
        )}
      </div>
    </section>
  );
}

function Market({ m, now, slots }: { m: OpenMarket; now: number | null; slots: Record<string, number> }) {
  const left = now == null ? (m.close_ts - m.open_ts) / 1000 : (m.close_ts - now) / 1000;
  const span = (m.close_ts - m.open_ts) / 1000;
  const frac = Math.min(1, Math.max(0, left / span));
  const closing = frac < 0.15;
  return (
    <motion.div className={`market ${closing ? "closing" : ""}`} layout
      initial={{ opacity: 0, x: 24 }} animate={{ opacity: 1, x: 0 }} exit={{ opacity: 0, x: -24, transition: { duration: 0.2 } }}
      transition={{ type: "spring", stiffness: 380, damping: 32 }}>
      <div className="name" title={marketName(m.id)}>{seriesName(m.series)} · closes {clock(m.close_ts, false)}</div>
      <div className="clock">{duration(left)}</div>
      <div className="quote"><span>bid {cents(m.bid)}</span><span>ask {cents(m.ask)}</span></div>
      {m.held.length > 0 && (
        <div className="held">
          {m.held.map((h) => (
            <span key={`${h.instance}-${h.side}`} className="chip">
              <i className="swatch" style={{ ["--c" as string]: `var(--series-${slots[h.instance] ?? 1})` }} />
              {h.instance} {h.qty} {h.side.toUpperCase()} @ {cents(h.avg)}
            </span>
          ))}
        </div>
      )}
      <div className="drain" style={{ width: `${frac * 100}%` }} />
    </motion.div>
  );
}

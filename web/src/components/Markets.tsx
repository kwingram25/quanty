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
  return (
    <motion.div className={`market ${closing ? "closing" : ""}`} layout
      initial={{ opacity: 0, x: 24 }} animate={{ opacity: 1, x: 0 }} exit={{ opacity: 0, x: -24, transition: { duration: 0.2 } }}
      transition={{ type: "spring", stiffness: 380, damping: 32 }}>
      <div className="top">
        <span className="clock">{duration(left)}</span>
        {!ladder && <span className="quote"><span>bid {cents(m0.bid)}</span><span>ask {cents(m0.ask)}</span></span>}
      </div>
      <div className="title" title={m0.id}>{m0.title}</div>
      <div className="name">
        {seriesName(m0.series)} · closes {closesAt(m0.close_ts, now)}
        {ref != null && <> · now <b className="num">{fmtNum(ref)}{m0.unit}</b></>}
        {!ladder && m0.strike != null && <> · strike <span className="num">{fmtNum(m0.strike)}</span></>}
      </div>
      {ladder ? (
        <div className="outcomes">
          {ms.map((m) => (
            <div key={m.id} className="outcome">
              <span className="label">{m.subtitle || m.id}</span>
              <span className="quote"><span>{cents(m.bid)}</span><span>{cents(m.ask)}</span></span>
              <Held m={m} slots={slots} />
            </div>
          ))}
        </div>
      ) : (
        <Held m={m0} slots={slots} />
      )}
      {exposure && (
        <div className="exposure" title="Across this question's outcomes: what could still be lost, and the most it could pay">
          {Object.entries(exposure).map(([iid, x]) => (
            <span key={iid} className="chip">
              <i className="swatch" style={{ ["--c" as string]: `var(--series-${slots[iid] ?? 1})` }} />
              {iid} worst <span className="down">{money(-x.loss)}</span> · best <span className="up">{money(x.gain, true)}</span>
            </span>
          ))}
        </div>
      )}
      <div className="drain" style={{ width: `${frac * 100}%` }} />
    </motion.div>
  );
}

function Held({ m, slots }: { m: OpenMarket; slots: Record<string, number> }) {
  if (!m.held.length) return null;
  return (
    <div className="held">
      {m.held.map((h) => (
        <span key={`${h.instance}-${h.side}`} className="chip">
          <i className="swatch" style={{ ["--c" as string]: `var(--series-${slots[h.instance] ?? 1})` }} />
          {h.instance} {h.qty} {h.side.toUpperCase()} @ {cents(h.avg)}
        </span>
      ))}
    </div>
  );
}

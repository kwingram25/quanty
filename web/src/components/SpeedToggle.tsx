import { motion } from "motion/react";
import { useEffect, useState } from "react";
import { sendCommand, type SessionState } from "../api";
import { useToast } from "./Toasts";

/** A demo's playback speed: fast or real time. Only demo sessions report a speed. */
export function SpeedToggle({ run, session }: { run: string; session: SessionState | null }) {
  const toast = useToast();
  const [pending, setPending] = useState<number | null>(null);
  const sp = session?.speed;
  // the choice shows at once and stays until the session reports it (the command's reply comes
  // before the next pushed snapshot, so clearing it then would flick back to the old speed)
  useEffect(() => {
    if (pending != null && sp?.value === pending) setPending(null);
  }, [sp?.value, pending]);
  useEffect(() => {
    if (pending == null) return;
    const t = setTimeout(() => setPending(null), 5000); // never confirmed: show what the session says
    return () => clearTimeout(t);
  }, [pending]);
  if (!sp || sp.options.length < 2) return null;
  const current = pending ?? sp.value;
  const pick = async (v: number) => {
    if (v === current) return;
    setPending(v);
    const r = await sendCommand(run, "speed", String(v));
    if (!r.ok) {
      toast({ kind: "error", title: "Speed not changed", body: r.result });
      setPending(null);
    }
  };
  return (
    <div className="segmented" role="radiogroup" aria-label="Demo speed">
      {sp.options.map((v) => (
        <button key={v} role="radio" aria-checked={current === v} onClick={() => pick(v)}
          title={v === 1 ? "Markets move at their real pace" : `Markets move ${v}x faster than real time`}>
          {current === v && <motion.span className="thumb" layoutId="speed-thumb" transition={{ type: "spring", stiffness: 500, damping: 38 }} />}
          <span>{v === 1 ? "Real time" : `Fast ${v}×`}</span>
        </button>
      ))}
    </div>
  );
}

/** A demo's "start over": a freshly generated day of history, then live again. */
export function NewDay({ run, session }: { run: string; session: SessionState | null }) {
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const sp = session?.speed;
  if (!sp?.new_day) return null;
  const go = async () => {
    setBusy(true);
    const r = await sendCommand(run, "new_day");
    toast({ kind: r.ok ? "ok" : "error", title: r.ok ? "New day" : "Couldn't start a new day", body: r.result });
    setBusy(false);
  };
  return (
    <span className="new-day">
      <button className="btn" onClick={go} disabled={busy || !!sp.catching_up}
        title="Start over on a freshly generated day: new markets, a new day of history, then live">
        ↻ New day
      </button>
      {sp.seed != null && <span className="muted" title={`quanty demo --live --seed ${sp.seed} replays this day`}>seed {sp.seed}</span>}
    </span>
  );
}

import { motion } from "motion/react";
import { useState } from "react";
import { sendCommand, type SessionState } from "../api";
import { useToast } from "./Toasts";

/** A demo's playback speed: fast or real time. Only demo sessions report a speed. */
export function SpeedToggle({ run, session }: { run: string; session: SessionState | null }) {
  const toast = useToast();
  const [pending, setPending] = useState<number | null>(null);
  const sp = session?.speed;
  if (!sp || sp.options.length < 2) return null;
  const current = pending ?? sp.value;
  const pick = async (v: number) => {
    if (v === current) return;
    setPending(v);
    const r = await sendCommand(run, "speed", String(v));
    if (!r.ok) toast({ kind: "error", title: "Speed not changed", body: r.result });
    setPending(null);
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

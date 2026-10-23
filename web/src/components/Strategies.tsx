import { AnimatePresence, motion, useAnimationControls, useReducedMotion } from "motion/react";
import { useEffect, useRef, useState } from "react";
import { sendCommand, type SessionState, type Snapshot, type Strategy } from "../api";
import { money, tone } from "../format";
import { AnimatedNumber } from "./AnimatedNumber";
import { useToast } from "./Toasts";

/** Strategies with their P&L; for a running session, also its controls. */
export function Strategies({ snap, slots }: { snap: Snapshot; slots: Record<string, number> }) {
  const s = snap.session;
  const run = snap.run.name;
  const toast = useToast();
  const [pending, setPending] = useState<Record<string, boolean>>({}); // optimistic halted state
  const [confirm, setConfirm] = useState<null | { action: string; target: string; label: string }>(null);

  const act = async (action: string, target: string, label: string, confirmText = "") => {
    if (snap.run.mode === "live" && !confirmText) { setConfirm({ action, target, label }); return; }
    if (action === "halt" || action === "resume") setPending((p) => ({ ...p, [target]: action === "halt" }));
    const r = await sendCommand(run, action, target, confirmText);
    const same = r.result.trim().toLowerCase() === label.trim().toLowerCase();
    toast({ kind: r.ok ? "ok" : "error", title: r.ok ? label : `${label} failed`, body: same ? undefined : r.result });
    setPending((p) => { const { [target]: _, ...rest } = p; return rest; });
  };

  return (
    <aside className="panel side fill">
      <header><h2>Strategies</h2></header>
      <div className="panel-body scroll">
        {snap.strategies.length === 0 && <p className="empty">No strategies have traded yet.</p>}
        {snap.strategies.map((st) => {
          const halted = pending[st.instance] ?? s?.instances[st.instance]?.halted ?? false;
          return <Row key={st.instance} st={st} slot={slots[st.instance] ?? 1} session={s} halted={halted}
            blocked={s?.instances[st.instance]?.blocked ?? 0}
            onToggle={() => act(halted ? "resume" : "halt", st.instance, `${halted ? "Resumed" : "Halted"} ${st.instance}`)} />;
        })}
        {s && <Brokers s={s} act={act} />}
      </div>
      {s && (
        <div className="panel-foot">
          <HoldButton onConfirm={() => act("kill", "", "Kill switch: everything halted")} />
          <p className="side-note">Halt stops new buys and cancels resting ones; exits still go through.</p>
        </div>
      )}
      <AnimatePresence>
        {confirm && <ConfirmDialog name={run.split("/")[1]} label={confirm.label}
          onCancel={() => setConfirm(null)}
          onConfirm={(text) => { const c = confirm; setConfirm(null); act(c.action, c.target, c.label, text); }} />}
      </AnimatePresence>
    </aside>
  );
}

function Row({ st, slot, session, halted, blocked, onToggle }: {
  st: Strategy; slot: number; session: SessionState | null; halted: boolean; blocked: number; onToggle: () => void;
}) {
  const prev = useRef(st.pnl);
  const [delta, setDelta] = useState<{ v: number; k: number } | null>(null);
  const flash = useAnimationControls();
  const reduce = useReducedMotion();
  useEffect(() => {
    const d = st.pnl - prev.current;
    prev.current = st.pnl;
    if (Math.abs(d) < 0.005) return;
    setDelta({ v: d, k: Date.now() });
    if (!reduce) {
      flash.start({
        backgroundColor: [d > 0 ? "var(--up-wash)" : "var(--down-wash)", "rgba(0,0,0,0)"],
        transition: { duration: 1.4 },
      });
    }
  }, [st.pnl, flash, reduce]);
  return (
    <motion.div className={`strategy ${halted ? "halted" : ""}`} animate={flash} style={{ borderRadius: 8 }}>
      <div className="top">
        <span className="name"><i className="swatch" style={{ ["--c" as string]: `var(--series-${slot})` }} />{st.instance}</span>
        <AnimatedNumber className={`pnl ${tone(st.pnl)}`} value={st.pnl} format={(v) => money(v)} />
        <AnimatePresence>
          {delta && (
            <motion.span key={delta.k} className={`delta ${tone(delta.v)}`}
              initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: -4 }} exit={{ opacity: 0, y: -12 }}
              transition={{ duration: 0.5 }} onAnimationComplete={() => setTimeout(() => setDelta(null), 900)}>
              {money(delta.v, true)}
            </motion.span>
          )}
        </AnimatePresence>
      </div>
      <div className="sub">
        <span className="grow" title={`${st.wins} won, ${st.losses} lost${st.rejected ? `, ${st.rejected} orders rejected` : ""}`}>
          {st.fills} fills · {st.wins}–{st.losses}{st.rejected ? ` · ${st.rejected} rej` : ""}
          {halted && blocked > 0 && <span className="blocked" title="buys refused while halted"> · {blocked} blocked</span>}</span>
        {session && session.instances[st.instance] && <>
          <span className={`pill ${halted ? "halted" : "trading"}`}><span className="dot" />{halted ? "halted" : "trading"}</span>
          <button className="btn" onClick={onToggle}>{halted ? "Resume" : "Halt"}</button>
        </>}
      </div>
    </motion.div>
  );
}

function Brokers({ s, act }: { s: SessionState; act: (a: string, t: string, l: string) => void }) {
  const live = Object.entries(s.brokers).filter(([, b]) => b.kind === "live");
  if (!live.length) return null;
  return (
    <>
      <h3>Live brokers</h3>
      {live.map(([name, b]) => (
        <div key={name} className="strategy">
          <div className="top"><span className="name">{name}</span>
            <span className={`pill ${b.disabled ? "disabled" : b.armed ? "armed" : "halted"}`}><span className="dot" />
              {b.disabled ? "disabled" : b.armed ? "armed" : "disarmed"}</span></div>
          <div className="sub"><span className="grow">{b.disabled}</span>
            {b.armed ? <button className="btn" onClick={() => act("disarm", name, `Disarmed ${name}`)}>Disarm</button>
              : s.can_arm && !b.disabled ? <button className="btn" onClick={() => act("arm", name, `Armed ${name}`)}>Arm</button> : null}
          </div>
        </div>
      ))}
    </>
  );
}

/** Press and hold to confirm: a stray click can't halt everything. */
function HoldButton({ onConfirm, ms = 1200 }: { onConfirm: () => void; ms?: number }) {
  const [holding, setHolding] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout>>();
  const start = () => { setHolding(true); timer.current = setTimeout(() => { setHolding(false); onConfirm(); }, ms); };
  const stop = () => { setHolding(false); clearTimeout(timer.current); };
  return (
    <button className="kill" onPointerDown={start} onPointerUp={stop} onPointerLeave={stop}
      onKeyDown={(e) => { if ((e.key === "Enter" || e.key === " ") && !e.repeat && !holding) start(); }}
      onKeyUp={stop} aria-label="Kill switch: press and hold to halt every strategy">
      <motion.span className="fill" initial={{ scaleX: 0 }} animate={{ scaleX: holding ? 1 : 0 }}
        transition={{ duration: holding ? ms / 1000 : 0.2, ease: "linear" }} />
      <span>{holding ? "Keep holding…" : "Hold to halt everything"}</span>
    </button>
  );
}

function ConfirmDialog({ name, label, onCancel, onConfirm }: {
  name: string; label: string; onCancel: () => void; onConfirm: (text: string) => void;
}) {
  const [text, setText] = useState("");
  return (
    <motion.div className="scrim" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} onClick={onCancel}>
      <motion.form className="dialog" role="dialog" aria-modal="true" onClick={(e) => e.stopPropagation()}
        initial={{ y: 12, scale: 0.98 }} animate={{ y: 0, scale: 1 }} exit={{ y: 12, opacity: 0 }}
        onSubmit={(e) => { e.preventDefault(); if (text === name) onConfirm(text); }}>
        <h3>This is a live run</h3>
        <p>{label.replace(/^(Halted|Resumed|Armed|Disarmed)/, (m) => ({ Halted: "Halt", Resumed: "Resume", Armed: "Arm", Disarmed: "Disarm" }[m] ?? m))} on real money. Type <b>{name}</b> to confirm.</p>
        <input autoFocus value={text} onChange={(e) => setText(e.target.value)} placeholder={name} aria-label="Run name" />
        <div className="actions">
          <button type="button" className="btn" onClick={onCancel}>Cancel</button>
          <button type="submit" className="btn danger" disabled={text !== name}>Confirm</button>
        </div>
      </motion.form>
    </motion.div>
  );
}

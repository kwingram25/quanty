import { AnimatePresence, motion, useAnimationControls, useReducedMotion } from "motion/react";
import { useEffect, useRef, useState } from "react";
import { sendCommand, type SessionState, type Snapshot, type Strategy, type StrategyInfo } from "../api";
import { cents, money, seriesName, tone } from "../format";
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
    if (!r.ok) drop(target); // otherwise kept until the session reports it, so the row doesn't flick back
  };
  const drop = (target: string) => setPending((p) => { const { [target]: _, ...rest } = p; return rest; });
  useEffect(() => {
    for (const [iid, want] of Object.entries(pending)) {
      if (s?.instances[iid]?.halted === want) drop(iid);
    }
  }, [s, pending]);
  useEffect(() => {
    if (!Object.keys(pending).length) return;
    const t = setTimeout(() => setPending({}), 5000); // never confirmed: show what the session says
    return () => clearTimeout(t);
  }, [pending]);

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
  const [open, setOpen] = useState(() => {
    try { return sessionStorage.getItem(`open:${st.instance}`) === "1"; } catch { return false; }
  });
  useEffect(() => {
    try { sessionStorage.setItem(`open:${st.instance}`, open ? "1" : "0"); } catch { /* private mode */ }
  }, [open, st.instance]);
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
        <button className="name expander" aria-expanded={open} onClick={() => setOpen(!open)}
          title={open ? "Hide details" : st.info?.description || "Show details"}>
          <motion.span className="chev" animate={{ rotate: open ? 90 : 0 }} transition={{ duration: 0.15 }}>›</motion.span>
          <i className="swatch" style={{ ["--c" as string]: `var(--series-${slot})` }} />{st.instance}
        </button>
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
      <AnimatePresence initial={false}>
        {open && (
          <motion.div className="details" initial={{ height: 0, opacity: 0 }} animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }} transition={{ duration: 0.2, ease: [0.22, 1, 0.36, 1] }}>
            <Details st={st} session={session} />
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  );
}

const fmtParam = (v: unknown): string =>
  Array.isArray(v) ? v.join(", ") : typeof v === "number" ? String(+v.toFixed(6)) : v == null ? "–" : String(v);

/** Everything worth knowing about one strategy instance. */
function Details({ st, session }: { st: Strategy; session: SessionState | null }) {
  // tolerate ledgers whose details were stored in another shape
  const raw = st.info ?? {};
  const obj = (v: unknown) => (v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, never>) : undefined);
  const info: StrategyInfo = {
    ...raw,
    series: Array.isArray(raw.series) ? raw.series : undefined,
    params_set: Array.isArray(raw.params_set) ? raw.params_set : undefined,
    params: obj(raw.params), param_help: obj(raw.param_help), limits: obj(raw.limits),
  };
  const settled = st.wins + st.losses;
  const holding = (session?.markets ?? []).flatMap((m) =>
    m.held.filter((h) => h.instance === st.instance).map((h) => ({ m, h })));
  const set = new Set(info.params_set ?? []);
  const params = Object.entries(info.params ?? {}).filter(([k]) => k !== "series")
    .sort(([a], [b]) => Number(set.has(b)) - Number(set.has(a)));
  const limits = Object.entries(info.limits ?? {});
  return (
    <div className="details-body">
      {info.description && <p className="desc">{info.description}</p>}
      {info.strategy && <p className="muted small">strategy <code>{info.strategy}</code>{info.broker && <> · broker <code>{info.broker}</code></>}</p>}

      <h4>Markets it trades</h4>
      <div className="chips">
        {(info.series?.length ? info.series : ["every market"]).map((s) => (
          <span key={s} className="chip" title={s}>{s === "every market" ? s : seriesName(s)}</span>
        ))}
      </div>

      {session && (
        <>
          <h4>Holding now</h4>
          {holding.length ? (
            <ul className="holding">
              {holding.map(({ m, h }) => (
                <li key={m.id + h.side}><span>{m.title}{m.subtitle && <span className="muted"> · {m.subtitle}</span>}</span>
                  <span className="num">{h.qty} {h.side === "yes" ? "Yes" : "No"} at {cents(h.avg)}</span></li>
              ))}
            </ul>
          ) : <p className="muted small">Nothing open.</p>}
        </>
      )}

      <h4>Results</h4>
      <dl className="stats-grid">
        <div><dt>Win rate</dt><dd>{settled ? `${Math.round((st.wins / settled) * 100)}%` : "–"}</dd></div>
        <div><dt>Markets traded</dt><dd>{info.markets_traded ?? "–"}</dd></div>
        <div><dt>Avg per market</dt><dd className={tone(settled ? st.pnl / settled : 0)}>{settled ? money(st.pnl / settled, true) : "–"}</dd></div>
        <div><dt>Contracts</dt><dd>{+st.contracts.toFixed(2)}</dd></div>
        <div><dt>Fees</dt><dd>{money(st.fees)}</dd></div>
        <div><dt>Orders</dt><dd>{st.orders}{st.rejected ? <span className="muted"> ({st.rejected} rejected)</span> : null}</dd></div>
      </dl>

      {params.length > 0 && (
        <>
          <h4>Parameters <span className="muted small">set by the experiment in bold</span></h4>
          <dl className="params">
            {params.map(([k, v]) => (
              <div key={k} className={set.has(k) ? "set" : ""} title={info.param_help?.[k] || undefined}>
                <dt>{k}</dt><dd className="num">{fmtParam(v)}</dd>
              </div>
            ))}
          </dl>
        </>
      )}
      {limits.length > 0 && (
        <>
          <h4>Limits</h4>
          <dl className="params">
            {limits.map(([k, v]) => <div key={k} className="set"><dt>{k}</dt><dd className="num">{fmtParam(v)}</dd></div>)}
          </dl>
        </>
      )}
      {!st.info && <p className="muted small">This ledger predates recorded strategy details.</p>}
    </div>
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

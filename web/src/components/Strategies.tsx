import { AnimatePresence, motion, useAnimationControls, useReducedMotion } from "motion/react";
import { useEffect, useRef, useState } from "react";
import { sendCommand, type Broker, type SessionState, type Snapshot, type Strategy, type StrategyInfo } from "../api";
import { cents, money, seriesName, tone } from "../format";
import { AnimatedNumber } from "./AnimatedNumber";
import { Id } from "./Id";
import { useToast } from "./Toasts";

/** Strategies with their P&L; for a running session, also its controls. */
export function Strategies({ snap, slots, onHover, focus }: {
  snap: Snapshot; slots: Record<string, number>; onHover?: (name: string | null) => void;
  /** the strategies the chart draws in color; the others' swatches are gray, as their lines are */
  focus?: Set<string>;
}) {
  const s = snap.session;
  const run = snap.run.name;
  const toast = useToast();
  const [pending, setPending] = useState<Record<string, boolean>>({}); // optimistic halted state
  type Pending = { action: string; target: string; label: string; value?: Record<string, unknown> };
  const [confirm, setConfirm] = useState<null | Pending>(null);

  /** Sends a command; true/false = its outcome, null = waiting for the live-run confirmation. */
  const act = async (action: string, target: string, label: string, confirmText = "",
    value?: Record<string, unknown>): Promise<boolean | null> => {
    if ((snap.needs_confirm || snap.real || snap.run.mode === "live") && !confirmText) { setConfirm({ action, target, label, value }); return null; }
    if (action === "halt" || action === "resume") setPending((p) => ({ ...p, [target]: action === "halt" }));
    const r = await sendCommand(run, action, target, confirmText, value);
    const same = r.result.trim().toLowerCase() === label.trim().toLowerCase();
    toast({ kind: r.ok ? "ok" : "error", title: r.ok ? label : `${label} failed`, body: same ? undefined : r.result });
    if (!r.ok) drop(target); // otherwise kept until the session reports it, so the row doesn't flick back
    return r.ok;
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
      <header><h2>Strategies</h2>
        {s?.reload && s.actions?.includes("reload") && (
          <button className="btn mini" onClick={() => act("reload", "", "Reloaded the experiment")}
            title="Pick up the experiment file without a restart: new instances start, removed ones retire (no new entries), changed params and brokers apply">
            Reload experiment</button>
        )}
      </header>
      <div className="panel-body scroll">
        {snap.strategies.length === 0 && <p className="empty">No strategies have traded yet.</p>}
        {[...snap.strategies].sort((a, b) => Number(snap.kinds?.[b.instance] === "live") - Number(snap.kinds?.[a.instance] === "live")).map((st) => {
          const halted = pending[st.instance] ?? s?.instances[st.instance]?.halted ?? false;
          return <Row key={st.instance} st={st} slot={!focus || focus.has(st.instance) ? slots[st.instance] ?? 1 : 0} session={s} halted={halted}
            kind={snap.kinds?.[st.instance] ?? "sim"} onHover={onHover} byKind={snap.pnl_kinds?.[st.instance]}
            blocked={s?.instances[st.instance]?.blocked ?? 0} stops={snap.order_stats?.[st.instance]?.stops ?? 0}
            onToggle={() => act(halted ? "resume" : "halt", st.instance, `${halted ? "Resumed" : "Halted"} ${st.instance}`)}
            onEdit={(value) => act("params", st.instance, `Changed ${st.instance}`, "", value)}
            onReset={() => act("reset_params", st.instance, `Reset ${st.instance} to the experiment file`)}
            onSave={() => act("save_params", st.instance, `Saved ${st.instance} to the experiment file`)}
            onSwitch={(broker, kind) => act("switch", st.instance,
              kind === "live" ? `Switched ${st.instance} to live on ${broker}` : kind === "shadow"
                ? `Switched ${st.instance} to shadow on ${broker}` : `Switched ${st.instance} back to paper`, "", { broker, twin: true })} />;
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
          onConfirm={(text) => { const c = confirm; setConfirm(null); act(c.action, c.target, c.label, text, c.value); }} />}
      </AnimatePresence>
    </aside>
  );
}

/** Real money or not, on every strategy: LIVE (real orders), shadow (checked by the venue, fills
 * simulated) or paper (simulated). */
export function KindBadge({ kind }: { kind: string }) {
  const [label, title] = kind === "live" ? ["LIVE", "Real orders, real money"]
    : kind === "shadow" ? ["shadow", "Orders checked by the venue but not placed; fills simulated"]
    : ["paper", "Simulated fills; no orders reach the venue"];
  return <span className={`kind-badge ${kind}`} title={title}>{label}</span>;
}

type Edit = (value: Record<string, unknown>) => Promise<boolean | null>;

function Row({ st, slot, kind, onHover, byKind, session, halted, blocked, stops, onToggle, onEdit, onReset, onSave, onSwitch }: {
  st: Strategy; slot: number; kind: string; onHover?: (name: string | null) => void;
  /** its realized P&L by kind: a strategy promoted mid-run has paper and real-money parts */
  byKind?: Record<string, number>; session: SessionState | null; halted: boolean; blocked: number; stops: number;
  onToggle: () => void;
  onEdit: Edit; onReset: () => Promise<boolean | null>; onSave: () => Promise<boolean | null>;
  onSwitch: (broker: string, kind: string) => Promise<boolean | null>;
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
    <motion.div className={`strategy ${halted ? "halted" : ""} ${kind === "live" ? "real" : ""}`} animate={flash} style={{ borderRadius: 8 }}
      onMouseEnter={() => onHover?.(st.instance)} onMouseLeave={() => onHover?.(null)}>
      <div className="top">
        <button className="name expander" aria-expanded={open} onClick={() => setOpen(!open)}
          title={[st.instance, open ? "Hide details" : st.info?.description || "Show details"].join(": ")}>
          <motion.span className="chev" animate={{ rotate: open ? 90 : 0 }} transition={{ duration: 0.15 }}>›</motion.span>
          <i className="swatch" style={{ ["--c" as string]: `var(--series-${slot})` }} /><span className="label"><Id name={st.instance} /></span>
        </button>
        <KindBadge kind={kind} />
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
          {stops > 0 && <span className="stops" title="positions sold by its stop loss"> · {stops} stop{stops > 1 ? "s" : ""}</span>}
          {byKind && "live" in byKind && Object.keys(byKind).length > 1 && (
            <span className="real-part" title="Of its P&L: what it made with real money, after going live (the rest was simulated)">
              {" "}· real {money(byKind.live, true)}</span>
          )}
          {halted && blocked > 0 && <span className="blocked" title="buys refused while halted"> · {blocked} blocked</span>}</span>
        {session && session.instances[st.instance] && (session.instances[st.instance].retired ? (
          <span className="pill halted" title="Removed from the experiment: no new entries; what it holds finishes. Gone after a restart">
            <span className="dot" />retired</span>
        ) : <>
          <span className={`pill ${halted ? "halted" : "trading"}`}><span className="dot" />{halted ? "halted" : "trading"}</span>
          <button className="btn" onClick={onToggle}>{halted ? "Resume" : "Halt"}</button>
        </>)}
      </div>
      <AnimatePresence initial={false}>
        {open && (
          <motion.div className="details" initial={{ height: 0, opacity: 0 }} animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }} transition={{ duration: 0.2, ease: [0.22, 1, 0.36, 1] }}>
            <Details st={st} session={session} onEdit={onEdit} onReset={onReset} onSave={onSave} onSwitch={onSwitch} />
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  );
}

const fmtParam = (v: unknown): string =>
  Array.isArray(v) ? v.join(", ") : typeof v === "number" ? String(+v.toFixed(6)) : v == null ? "–" : String(v);

/** Everything worth knowing about one strategy instance. */
function Details({ st, session, onEdit, onReset, onSave, onSwitch }: {
  st: Strategy; session: SessionState | null; onEdit: Edit; onReset: () => Promise<boolean | null>;
  onSave: () => Promise<boolean | null>; onSwitch: (broker: string, kind: string) => Promise<boolean | null>;
}) {
  const toast = useToast();
  const [editing, setEditing] = useState(false);
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
  const running = session?.instances[st.instance];
  const canEdit = !!running && !!session?.actions?.includes("params");
  const current = obj(running?.params) ?? info.params ?? {}; // a running session's are the latest
  const editedList = running?.edited ?? (Array.isArray(raw.edited) ? raw.edited : []);
  const edited = new Set(editedList);
  const params = Object.entries(current).filter(([k]) => k !== "series")
    .sort(([a], [b]) => Number(set.has(b)) - Number(set.has(a)));
  const limits = Object.entries(info.limits ?? {});
  return (
    <div className="details-body">
      {info.description && <p className="desc">{info.description}</p>}
      {info.strategy && <p className="muted small">strategy <code>{info.strategy}</code>{info.broker && <> · broker <code>{info.broker}</code></>}</p>}
      {info.code && (
        <p className="muted small" title="The code this run started with: the project's git commit (or the one quanty deploy sent), and a hash of the strategy's file, which changes with any edit">
          code {info.code.commit ? <><code>{info.code.commit}</code>{info.code.dirty && " with uncommitted changes"}</> : "not in git"}
          {info.code.file && <> · file <code>{info.code.file}</code></>}{info.code.quanty && <> · quanty {info.code.quanty}</>}
        </p>
      )}

      {session && <SwitchBroker iid={st.instance} strategy={info.strategy} series={info.series ?? []} session={session} onSwitch={onSwitch} />}

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
          <h4 className="with-action">
            <span>Parameters <span className="muted small">
              {edited.size ? <><i className="edited-dot" /> changed here</> : "set by the experiment in bold"}</span></span>
            {canEdit && !editing && <span className="head-actions">
              {edited.size > 0 && session?.save === "file" && session.actions?.includes("save_params") && (
                <button className="btn mini" onClick={() => onSave()}
                  title="Write these changes into the experiment file, so restarts and backtests use them too">Save to file</button>
              )}
              {edited.size > 0 && session?.save === "copy" && (
                <button className="btn mini" title="This session runs from a container image, whose copy of the experiment file a deploy replaces: copy the lines for the file on your machine"
                  onClick={async () => {
                    const text = paramsToml(st.instance, Object.fromEntries([...edited].map((k) => [k, current[k]])));
                    try {
                      await navigator.clipboard.writeText(text);
                      toast({ kind: "ok", title: "Copied for the experiment file", body: `Paste under the [[instances]] entry with id = "${st.instance}", then deploy.` });
                    } catch {
                      toast({ kind: "warning", title: "Copy these lines into the experiment file", body: text });
                    }
                  }}>Copy for the file</button>
              )}
              <button className="btn mini" onClick={() => setEditing(true)}>Edit</button>
            </span>}
          </h4>
          {editing ? (
            <ParamEditor params={params} help={info.param_help ?? {}} edited={edited}
              onCancel={() => setEditing(false)}
              onSave={async (changes) => { if ((await onEdit(changes)) !== false) setEditing(false); }}
              onReset={async () => { if ((await onReset()) !== false) setEditing(false); }} />
          ) : (
            <dl className="params">
              {params.map(([k, v]) => (
                <div key={k} className={`${set.has(k) ? "set" : ""} ${edited.has(k) ? "edited" : ""}`}
                  title={[info.param_help?.[k], edited.has(k) ? "changed from the dashboard" : ""].filter(Boolean).join(" · ") || undefined}>
                  <dt>{k}</dt><dd className="num">{fmtParam(v)}</dd>
                </div>
              ))}
            </dl>
          )}
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

/** Going live, shadow or back to paper while the session runs: every experiment has the sim, shadow
 * and live brokers (live only in a session started with `quanty live --arm`, and only once its caps
 * are set in the experiment file). New markets use the new broker; the markets the strategy is in
 * finish where they are. Going live also starts its paper twin, for Compare. */
function SwitchBroker({ iid, strategy, series, session, onSwitch }: {
  iid: string; strategy?: string; series: string[]; session: SessionState;
  onSwitch: (broker: string, kind: string) => Promise<boolean | null>;
}) {
  const toast = useToast();
  const current = session.instances[iid]?.broker;
  const held = session.instances[iid]?.switched;
  if (!current || !session.actions?.includes("switch")) return null;
  const kind = session.brokers[current]?.kind;
  const others = Object.entries(session.brokers).filter(([n]) => n !== current);
  const live = others.filter(([, b]) => b.kind === "live");
  const shadow = others.filter(([, b]) => b.kind === "shadow");
  const paper = others.filter(([, b]) => b.kind === "sim");
  const venues = new Set(series.map((x) => x.split(":")[0]));
  const noKeys = (b: Broker) => (b.keys_missing ?? []).filter((v) => venues.has(v));
  // why Go live is off, if it is (a demo has no live broker, and needs no note)
  const whyNotLive = (b: Broker) =>
    !session.can_arm ? "Real money needs a session started with quanty live --arm"
      : b.capped === false ? "Set max_qty and max_notional under [brokers.live] in the experiment file (the largest single order), then Reload experiment"
        : b.disabled ? `Disabled itself: ${b.disabled}. Check the venue, then restart`
          : !b.armed ? "Disarmed: arm it under Live brokers" : "";
  const lines = held && [
    `# in the [[instances]] entry with id = "${iid}"`, `broker = "${held.broker}"`,
    ...(held.twin ? ["", "# its paper twin, a new entry", "[[instances]]", `id = "${held.twin}"`,
      `strategy = "${strategy ?? "?"}"`, `broker = "${session.instances[held.twin]?.broker ?? "?"}"`, `twin_of = "${iid}"`] : []),
  ].join("\n");
  const liveOff = kind !== "live" && session.save != null && (live.length ? whyNotLive(live[0][1]) : session.can_arm ? "" : "Real money needs a session started with quanty live --arm");
  return (
    <div className="switch-broker">
      <h4>Broker <span className="muted small">{current} ({kind === "live" ? "real money" : kind})</span></h4>
      {held && (
        <p className="muted small held">Switched on this server, not yet in your experiment file: <code>quanty pull</code> (or the
          next <code>quanty deploy</code>) writes it there.{" "}
          <button className="btn mini" onClick={async () => {
            try { await navigator.clipboard.writeText(lines!); toast({ kind: "ok", title: "Copied for the experiment file" }); }
            catch { toast({ kind: "warning", title: "Copy these lines into the experiment file", body: lines! }); }
          }}>Copy for the file</button></p>
      )}
      <div className="row-actions">
        {kind !== "shadow" && kind !== "live" && shadow.map(([n, b]) => {
          const missing = noKeys(b);
          return (
            <button key={n} className="btn" disabled={missing.length > 0} onClick={() => onSwitch(n, "shadow")}
              title={missing.length ? `No ${missing.join(", ")} keys in .env: shadow checks each order with the venue's keys`
                : "Each order is checked by the venue (never placed) from the next market it enters; fills stay simulated"}>
              Shadow</button>
          );
        })}
        {kind !== "live" && live.map(([n, b]) => (
          <button key={n} className="btn danger-outline" disabled={!!whyNotLive(b)} onClick={() => onSwitch(n, "live")}
            title={whyNotLive(b) || "Real orders from the next market it enters; markets it's in now finish where they are. Keeps a paper twin running beside it, for Compare"}>
            Go live</button>
        ))}
        {kind !== "sim" && paper.slice(0, 1).map(([n]) => (
          <button key={n} className="btn" onClick={() => onSwitch(n, "sim")}
            title={kind === "live" ? "Simulated from the next market it enters; positions it holds for real finish on the venue" : "Simulated from the next market it enters"}>
            Back to paper</button>
        ))}
      </div>
      {liveOff && <p className="muted small">{liveOff.replace(/^Set /, "To go live: set ")}.</p>}
    </div>
  );
}

/** Changed params as TOML lines for an [[instances]] entry's params table. */
const paramsToml = (instance: string, values: Record<string, unknown>): string =>
  [`# in the [[instances]] entry with id = "${instance}"`, "[instances.params]",
    ...Object.entries(values).map(([k, v]) => `${k} = ${typeof v === "string" ? JSON.stringify(v) : String(v)}`)].join("\n");

/** Edits a running instance's params: numbers, switches and text (lists stay as configured). Only
 * what changed is sent; the session validates it with the strategy's own rules. */
function ParamEditor({ params, help, edited, onCancel, onSave, onReset }: {
  params: [string, unknown][]; help: Record<string, string>; edited: Set<string>;
  onCancel: () => void; onSave: (changes: Record<string, unknown>) => void; onReset: () => void;
}) {
  const editable = params.filter(([, v]) => ["number", "boolean", "string"].includes(typeof v));
  const [draft, setDraft] = useState<Record<string, string | boolean>>(() =>
    Object.fromEntries(editable.map(([k, v]) => [k, typeof v === "boolean" ? v : String(v)])));
  const original = Object.fromEntries(editable);
  const parsed: Record<string, unknown> = {};
  const invalid = new Set<string>();
  for (const [k, v] of Object.entries(draft)) {
    const was = original[k];
    const val = typeof was === "number" ? (String(v).trim() === "" ? NaN : Number(v)) : v;
    if (typeof val === "number" && !Number.isFinite(val)) invalid.add(k);
    else if (val !== was) parsed[k] = val;
  }
  const changed = Object.keys(parsed).length > 0;
  const fixed = params.filter(([, v]) => !["number", "boolean", "string"].includes(typeof v));
  return (
    <form className="param-edit" onSubmit={(e) => { e.preventDefault(); if (changed && !invalid.size) onSave(parsed); }}>
      {editable.map(([k, v]) => (
        <label key={k} className={invalid.has(k) ? "invalid" : k in parsed ? "dirty" : ""}>
          <span className="pname">{k}{edited.has(k) && <i className="edited-dot" title="changed from the dashboard" />}</span>
          {typeof v === "boolean" ? (
            <input type="checkbox" checked={draft[k] as boolean} onChange={(e) => setDraft({ ...draft, [k]: e.target.checked })} />
          ) : (
            <input type="text" inputMode={typeof v === "number" ? "decimal" : undefined} value={draft[k] as string}
              aria-invalid={invalid.has(k)} onChange={(e) => setDraft({ ...draft, [k]: e.target.value })} />
          )}
          {help[k] && <span className="phelp">{help[k]}</span>}
        </label>
      ))}
      {fixed.length > 0 && <p className="muted small">{fixed.map(([k]) => k).join(", ")}: change in the experiment file.</p>}
      <p className="muted small">Takes effect from the next market update. Order limits still apply.</p>
      <div className="actions">
        {edited.size > 0 && <button type="button" className="btn ghost" onClick={onReset}>Reset to file</button>}
        <span className="grow" />
        <button type="button" className="btn" onClick={onCancel}>Cancel</button>
        <button type="submit" className="btn primary" disabled={!changed || invalid.size > 0}>Save</button>
      </div>
    </form>
  );
}

function Brokers({ s, act }: { s: SessionState; act: (a: string, t: string, l: string) => unknown }) {
  const live = Object.entries(s.brokers).filter(([, b]) => b.kind === "live");
  if (!live.length) return null;
  return (
    <>
      <h3>Live brokers</h3>
      {live.map(([name, b]) => (
        <div key={name} className="strategy">
          <div className="top"><span className="name">{name}</span>
            <span className={`pill ${b.disabled ? "disabled" : b.capped === false ? "halted" : b.armed ? "armed" : "halted"}`}><span className="dot" />
              {b.disabled ? "disabled" : b.capped === false ? "no caps" : b.armed ? "armed" : "disarmed"}</span></div>
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
        <p>{label.replace(/^(Halted|Resumed|Armed|Disarmed|Changed|Switched)/, (m) => ({ Halted: "Halt", Resumed: "Resume", Armed: "Arm", Disarmed: "Disarm", Changed: "Change", Switched: "Switch" }[m] ?? m))}. This session trades real money: type <b>{name}</b> to confirm.</p>
        <input autoFocus value={text} onChange={(e) => setText(e.target.value)} placeholder={name} aria-label="Run name" />
        <div className="actions">
          <button type="button" className="btn" onClick={onCancel}>Cancel</button>
          <button type="submit" className="btn danger" disabled={text !== name}>Confirm</button>
        </div>
      </motion.form>
    </motion.div>
  );
}

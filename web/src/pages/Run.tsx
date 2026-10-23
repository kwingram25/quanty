import { motion } from "motion/react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useParams } from "react-router-dom";
import { useRunStream, type NewRows, type Snapshot } from "../api";
import { AnimatedNumber } from "../components/AnimatedNumber";
import { Markets } from "../components/Markets";
import { PnlChart } from "../components/PnlChart";
import { SpeedToggle } from "../components/SpeedToggle";
import { Strategies } from "../components/Strategies";
import { Tape } from "../components/Tape";
import { useToast } from "../components/Toasts";
import { ago, cents, clock, money, nameOf, slots as slotsOf, tone } from "../format";

const ALERTS = /disabled|outcome unknown|kill switch|leftover|never reported/i;

export function RunPage() {
  const { mode = "", name = "" } = useParams();
  const run = `${mode}/${name}`;
  const toast = useToast();
  const onNew = useCallback((n: NewRows) => {
    for (const raw of n.logs) {
      const l = raw as { level: string; msg: string; instance: string; fields: Record<string, unknown> };
      if (l.level === "error" || ALERTS.test(l.msg)) {
        const why = (l.fields.reason ?? l.fields.detail ?? l.fields.error ?? "") as string;
        toast({ kind: l.level === "error" ? "error" : "warning", title: l.msg, body: [l.instance, why].filter(Boolean).join(": ") });
      }
    }
  }, [toast]);
  const { snap, state } = useRunStream(run, onNew);

  if (state === "missing") return <div className="page"><p className="empty">No run named {run}.</p></div>;
  if (!snap) return <div className="page"><p className="empty">Loading {run}…</p></div>;
  return <Run snap={snap} live={state === "open"} />;
}

function Run({ snap, live }: { snap: Snapshot; live: boolean }) {
  const slots = useMemo(() => slotsOf(snap.strategies.map((s) => s.instance)), [snap.strategies]);
  const halted = useMemo(
    () => new Set(Object.entries(snap.session?.instances ?? {}).filter(([, v]) => v.halted).map(([k]) => k)),
    [snap.session],
  );
  const open = snap.positions.length;
  return (
    <div className="page run-page">
      <div className="run-head">
        <div className="run-title">
          <h1>{snap.run.name.split("/")[1]}
            <span className={`pill ${snap.run.mode}`}><span className="dot" />{snap.run.mode}</span>
            {snap.session && <span className="pill running" title={live ? "updates pushed live" : "reconnecting"}>
              <span className="dot" />running</span>}
            {snap.session?.platforms?.map((p) => (
              <span key={p.name} className={`pill platform ${p.status}`} title={`${p.name}: ${p.status}`}>
                <span className="dot" />{p.name.replace(/_/g, " ")}{p.status !== "ok" && <span className="muted"> · {p.status}</span>}
              </span>
            ))}
            <SpeedToggle run={snap.run.name} session={snap.session} />
            {snap.session?.speed?.catching_up && <span className="pill warning"><span className="dot" />loading history…</span>}
          </h1>
          <p>{snap.run.file} · last activity {ago(snap.run.last_ts)}</p>
        </div>
        <div className="kpis">
          <div className="kpi hero"><div className="label">Realized P&L</div>
            <AnimatedNumber className={`value ${tone(snap.stats.pnl)}`} value={snap.stats.pnl} format={(v) => money(v)} /></div>
          <div className="kpi"><div className="label">Fills</div><div className="value">{snap.stats.fills}</div></div>
          <div className="kpi"><div className="label">Open positions</div><div className="value">{open}</div></div>
          <div className="kpi"><div className="label">Errors</div>
            <div className={`value ${snap.stats.errors ? "down" : ""}`}>{snap.stats.errors}</div></div>
        </div>
      </div>

      <div className="workspace">
        <Strategies snap={snap} slots={slots} />
        <Center snap={snap} slots={slots} halted={halted} />
        <div className="right-col">
          <Markets session={snap.session} slots={slots} />
          <section className="panel fill">
            <header><h2>Tape</h2><span className="hint">fills, settlements and alerts</span></header>
            <div className="panel-body scroll"><Tape items={snap.tape} slots={slots} names={snap.names} /></div>
          </section>
        </div>
      </div>
    </div>
  );
}

const TABS = ["chart", "activity", "positions", "commands"] as const;
type Tab = (typeof TABS)[number];

const RANGES: Record<string, number | null> = { "1h": 3_600_000, "6h": 21_600_000, "24h": 86_400_000, All: null };

/** The center panel: the P&L chart, or one of the tables, in the same space. */
function Center({ snap, slots, halted }: { snap: Snapshot; slots: Record<string, number>; halted: Set<string> }) {
  const [tab, setTab] = useState<Tab>(() => {
    try {
      const t = sessionStorage.getItem("tab:run") as Tab;
      return TABS.includes(t) ? t : "chart";
    } catch { return "chart"; }
  });
  const pick = (t: Tab) => { setTab(t); try { sessionStorage.setItem("tab:run", t); } catch { /* private mode */ } };
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.target instanceof HTMLInputElement || e.metaKey || e.ctrlKey || e.altKey) return;
      const i = ["1", "2", "3", "4"].indexOf(e.key);
      if (i >= 0) pick(TABS[i]);
    };
    addEventListener("keydown", onKey);
    return () => removeEventListener("keydown", onKey);
  }, []);
  const counts: Record<Tab, number | null> = {
    chart: null, activity: snap.activity.length, positions: snap.positions.length, commands: snap.commands.length,
  };
  const label = (t: Tab) => (t === "chart" ? "P&L" : t[0].toUpperCase() + t.slice(1));
  const [range, setRange] = useState<string>(() => {
    try { const r = localStorage.getItem("chart:range"); return r && r in RANGES ? r : "All"; } catch { return "All"; }
  });
  const pickRange = (r: string) => { setRange(r); try { localStorage.setItem("chart:range", r); } catch { /* private mode */ } };
  return (
    <section className="panel center">
      <div className="tabbar" role="tablist">
        {TABS.map((t, i) => (
          <button key={t} role="tab" aria-selected={tab === t} onClick={() => pick(t)} title={`Press ${i + 1}`}>
            {label(t)}{counts[t] != null && <span className="count">{counts[t]}</span>}
            {tab === t && <motion.span className="underline" layoutId="tab-underline" />}
          </button>
        ))}
              {tab === "chart" && (
          <div className="segmented ranges" role="radiogroup" aria-label="Chart range">
            {Object.keys(RANGES).map((r) => (
              <button key={r} role="radio" aria-checked={range === r} onClick={() => pickRange(r)}>
                {range === r && <motion.span className="thumb" layoutId="range-thumb" transition={{ type: "spring", stiffness: 500, damping: 38 }} />}
                <span>{r}</span>
              </button>
            ))}
          </div>
        )}
      </div>
      <div className={tab === "chart" ? "chart-pane" : "table-wrap"} role="tabpanel">
        {tab === "chart" && <PnlChart curves={snap.curves} slots={slots} halted={halted} range={RANGES[range]}
          now={snap.session?.now ?? null} />}
        {tab === "activity" && <Activity snap={snap} />}
        {tab === "positions" && <Positions snap={snap} />}
        {tab === "commands" && <Commands snap={snap} />}
      </div>
    </section>
  );
}

function Activity({ snap }: { snap: Snapshot }) {
  if (!snap.activity.length) return <p className="empty" style={{ padding: 16 }}>No orders yet.</p>;
  return (
    <table>
      <thead><tr><th>Time</th><th>Strategy</th><th>Market</th><th>Order</th><th className="r">Limit</th>
        <th className="r">Filled</th><th className="r">Avg fill</th><th className="r">Fees</th><th>Liquidity</th>
        <th className="r">Realized</th><th>Status</th></tr></thead>
      <tbody>
        {snap.activity.map((a) => (
          <tr key={a.id}>
            <td className="muted">{clock(a.created_ts)}</td><td>{a.instance}</td><td><Name snap={snap} id={a.market_id} /></td>
            <td>{a.action} {a.side.toUpperCase()} {a.tif.toUpperCase()}{a.post_only ? " post-only" : ""}</td>
            <td className="r">{cents(a.price)}</td><td className="r">{a.filled} / {a.qty}</td>
            <td className="r">{a.avg_price == null ? "" : cents(a.avg_price)}</td>
            <td className="r">{a.fills ? money(a.fees) : ""}</td><td>{a.liquidity}</td>
            <td className={`r ${tone(a.realized)}`}>{a.realized ? money(a.realized, true) : ""}</td>
            <td>{a.status}{a.reason && <span className="why" title={a.reason}> · {a.reason.slice(0, 40)}</span>}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function Positions({ snap }: { snap: Snapshot }) {
  if (!snap.positions.length) return <p className="empty" style={{ padding: 16 }}>No open positions.</p>;
  return (
    <table>
      <thead><tr><th>Strategy</th><th>Market</th><th>Side</th><th className="r">Contracts</th><th className="r">Avg price</th></tr></thead>
      <tbody>
        {snap.positions.map((p) => (
          <tr key={`${p.instance}-${p.market_id}-${p.side}`}><td>{p.instance}</td><td><Name snap={snap} id={p.market_id} /></td>
            <td>{p.side.toUpperCase()}</td><td className="r">{p.qty}</td><td className="r">{cents(p.avg_price)}</td></tr>
        ))}
      </tbody>
    </table>
  );
}

function Commands({ snap }: { snap: Snapshot }) {
  if (!snap.commands.length) {
    return <p className="empty" style={{ padding: 16 }}>{snap.session ? "No commands sent yet." : "Controls appear while a session is running."}</p>;
  }
  return (
    <table>
      <thead><tr><th>Sent</th><th>Action</th><th>Target</th><th>Status</th><th>Result</th></tr></thead>
      <tbody>
        {snap.commands.map((c) => (
          <tr key={c.id}><td className="muted">{clock(c.created_ts)}</td><td>{c.action}</td><td>{c.target || "all"}</td>
            <td><span className={`pill ${c.status === "done" ? "ok" : c.status === "failed" ? "error" : "warning"}`}>
              <span className="dot" />{c.status}</span></td><td>{c.result}</td></tr>
        ))}
      </tbody>
    </table>
  );
}

function Name({ snap, id }: { snap: Snapshot; id: string }) {
  const n = nameOf(snap.names, id);
  return <span title={id}>{n.title}{n.subtitle && <span className="muted"> · {n.subtitle}</span>}</span>;
}

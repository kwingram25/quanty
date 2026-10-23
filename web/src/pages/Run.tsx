import { motion } from "motion/react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useParams } from "react-router-dom";
import { useRunStream, type ActivityRow, type NewRows, type OrderStats, type Snapshot } from "../api";
import { AnimatedNumber } from "../components/AnimatedNumber";
import { Markets } from "../components/Markets";
import { PnlChart } from "../components/PnlChart";
import { DemoClock, NewDay, SpeedToggle } from "../components/SpeedToggle";
import { Strategies } from "../components/Strategies";
import { Tape } from "../components/Tape";
import { useToast } from "../components/Toasts";
import { ago, cents, clock, money, nameOf, slots as slotsOf, tone } from "../format";

const ALERTS = /disabled|outcome unknown|kill switch|leftover|never reported/i;

export function RunPage() {
  const { mode = "", name = "" } = useParams();
  const run = `${mode}/${name}`;
  const toast = useToast();
  const onNew = useCallback((n: NewRows, s: Snapshot) => {
    if (s.session?.speed?.catching_up) return; // a demo loading its history: no callouts for the past
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
  // a demo's new day is a new run: start the page afresh (no "changes" animated from the old day)
  return <Run key={snap.session?.speed?.seed ?? "run"} snap={snap} live={state === "open"} />;
}

function Run({ snap, live }: { snap: Snapshot; live: boolean }) {
  const slots = useMemo(() => slotsOf(snap.strategies.map((s) => s.instance)), [snap.strategies]);
  const halted = useMemo(
    () => new Set(Object.entries(snap.session?.instances ?? {}).filter(([, v]) => v.halted).map(([k]) => k)),
    [snap.session],
  );
  const open = snap.positions.length;
  // a demo playing its day of history: the numbers are half-built until it's done, so show progress
  // (from the click on "New day": this page is replaced when the new day's snapshot arrives)
  const [starting, setStarting] = useState(false);
  useEffect(() => {
    const on = () => setStarting(true);
    const off = () => setStarting(false);
    addEventListener("quanty:new-day", on);
    addEventListener("quanty:new-day-failed", off);
    return () => { removeEventListener("quanty:new-day", on); removeEventListener("quanty:new-day-failed", off); };
  }, []);
  const loading = snap.session?.speed?.catching_up ? snap.session.speed.progress ?? 0 : starting ? 0 : null;
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
            <DemoClock session={snap.session} />
            <NewDay run={snap.run.name} session={snap.session} />

          </h1>
          <p>{snap.run.file} · last activity {ago(snap.run.last_ts)}</p>
        </div>
        {loading == null && <div className="kpis">
          <div className="kpi hero"><div className="label">Realized P&L</div>
            <AnimatedNumber className={`value ${tone(snap.stats.pnl)}`} value={snap.stats.pnl} format={(v) => money(v)} /></div>
          <div className="kpi"><div className="label">Fills</div><div className="value">{snap.stats.fills}</div></div>
          <div className="kpi"><div className="label">Open positions</div><div className="value">{open}</div></div>
          <div className="kpi"><div className="label">Errors</div>
            <div className={`value ${snap.stats.errors ? "down" : ""}`}>{snap.stats.errors}</div></div>
        </div>}
      </div>

      {loading != null ? <LoadingDay progress={loading} /> : <div className="workspace">
        <Strategies snap={snap} slots={slots} />
        <Center snap={snap} slots={slots} halted={halted} />
        <div className="right-col">
          <Markets session={snap.session} slots={slots} />
          <section className="panel fill">
            <header><h2>Tape</h2><span className="hint">fills, settlements and alerts</span></header>
            <div className="panel-body scroll"><Tape items={snap.tape} slots={slots} names={snap.names} /></div>
          </section>
        </div>
      </div>}
    </div>
  );
}

/** In place of the workspace while a demo plays its day of history (a few seconds). */
function LoadingDay({ progress }: { progress: number }) {
  const pct = Math.round(Math.min(1, Math.max(0, progress)) * 100);
  return (
    <div className="loading-day panel" role="status" aria-live="polite">
      <p>Playing the day's history so the dashboard opens on a full day…</p>
      <div className="bar" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={pct}>
        <motion.span initial={false} animate={{ width: `${pct}%` }} transition={{ duration: 0.3, ease: "easeOut" }} />
      </div>
      <p className="muted small">{pct}%</p>
    </div>
  );
}

const TABS = ["chart", "trades", "orders", "commands"] as const;
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
    chart: null, trades: snap.trades?.length ?? 0, orders: needsLook(snap.order_stats) ?? snap.activity.length,
    commands: snap.commands.length,
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
        {tab === "trades" && <Trades snap={snap} slots={slots} />}
        {tab === "orders" && <Orders snap={snap} slots={slots} />}
        {tab === "commands" && <Commands snap={snap} />}
      </div>
    </section>
  );
}

/** Orders that didn't simply fill, over every order (null before the ledger reported counts). */
const needsLook = (stats: OrderStats | undefined): number | null =>
  stats ? Object.values(stats).reduce((n, s) => n + s.orders - s.filled, 0) : null;

const RESULT: Record<ActivityRow["result"], string> = {
  filled: "Filled", partial: "Partly filled", missed: "Missed", unfilled: "Not filled", rejected: "Rejected", resting: "Resting",
};
const qty = (q: number) => String(+q.toFixed(2));

function resultDetail(a: ActivityRow): string {
  const at = a.avg_price != null ? ` at ${cents(a.avg_price)}` : "";
  switch (a.result) {
    case "filled": return `${qty(a.filled)}${at}`;
    case "partial": return `${qty(a.filled)} of ${qty(a.qty)}${at}; the rest ${a.status === "expired" ? "expired at the close" : "canceled"}`;
    case "missed": return a.action === "buy" ? `nothing offered at ${cents(a.price)} or less` : `no bid at ${cents(a.price)} or more`;
    case "unfilled": return a.status === "expired" ? "expired at the close" : "canceled before anything filled";
    case "rejected": return a.reason || "refused";
    case "resting": return `${qty(a.filled)} of ${qty(a.qty)} filled so far`;
  }
}

/** Execution: what each order asked for and how it turned out. By default only the orders worth a
 * look (missed, partly filled, rejected, still resting); what filled shows up in Trades. */
function Orders({ snap, slots }: { snap: Snapshot; slots: Record<string, number> }) {
  const [view, setView] = useState<"look" | "all">(() => {
    try { return localStorage.getItem("orders:view") === "all" ? "all" : "look"; } catch { return "look"; }
  });
  const pickView = (v: "look" | "all") => { setView(v); try { localStorage.setItem("orders:view", v); } catch { /* private mode */ } };
  const stats = snap.order_stats ?? {};
  const total = Object.values(stats).reduce(
    (t, s) => { for (const k of Object.keys(s) as (keyof typeof s)[]) t[k] = (t[k] ?? 0) + s[k]; return t; },
    {} as Record<string, number>);
  const look = needsLook(snap.order_stats) ?? 0;
  const rows = view === "look" ? snap.order_issues ?? [] : snap.activity;
  if (!snap.activity.length) return <p className="empty" style={{ padding: 16 }}>No orders yet.</p>;
  const perStrategy = Object.entries(stats).map(([k, s]) =>
    `${k}: ${s.orders} orders, ${s.filled} filled, ${s.missed} missed, ${s.partial} partly filled, ${s.rejected} rejected`).join("\n");
  const parts = (["missed", "partial", "unfilled", "rejected", "resting"] as const)
    .filter((k) => total[k]).map((k) => `${total[k]} ${RESULT[k].toLowerCase()}`);
  return (
    <>
      <div className="orders-head">
        <p className="trades-summary" title={perStrategy || undefined}>
          {total.orders} orders · <b>{total.orders ? Math.round((100 * (total.filled ?? 0)) / total.orders) : 0}%</b> filled in full
          {parts.length > 0 && <> · {parts.join(" · ")}</>}
        </p>
        <div className="segmented" role="radiogroup" aria-label="Which orders">
          {(["look", "all"] as const).map((v) => (
            <button key={v} role="radio" aria-checked={view === v} onClick={() => pickView(v)}
              title={v === "look" ? "Orders that didn't simply fill: missed, partly filled, rejected, resting" : "Every recent order"}>
              {view === v && <motion.span className="thumb" layoutId="orders-thumb" transition={{ type: "spring", stiffness: 500, damping: 38 }} />}
              <span>{v === "look" ? `Needs a look${look ? ` ${look}` : ""}` : "All"}</span>
            </button>
          ))}
        </div>
      </div>
      {rows.length === 0 ? (
        <p className="empty" style={{ padding: 16 }}>Every order filled in full. What they bought is in Trades.</p>
      ) : (
        <table className="trades orders">
          <thead><tr><th>Sent</th><th>Strategy</th><th>Market</th><th>Order</th><th>Result</th></tr></thead>
          <tbody>
            {rows.map((a) => (
              <tr key={a.id} className={`r-${a.result}`}>
                <td className="muted">{clock(a.created_ts)}</td>
                <td><span className="who"><i className="swatch" style={{ ["--c" as string]: `var(--series-${slots[a.instance] ?? 1})` }} />{a.instance}</span></td>
                <td className="market-cell" title={a.market_id}>
                  <span className="q">{nameOf(snap.names, a.market_id).title}</span>
                  <span className="muted">{nameOf(snap.names, a.market_id).subtitle}</span>
                </td>
                <td>
                  {a.action === "buy" ? "Buy" : "Sell"} <span className="num">{qty(a.qty)}</span>{" "}
                  <span className={`side ${a.side}`}>{a.side === "yes" ? "Yes" : "No"}</span>{" "}
                  <span className="num">at {cents(a.price)}</span>
                  <span className="muted cost">{[a.tif.toUpperCase(), a.post_only && "post-only", a.tag].filter(Boolean).join(" · ")}</span>
                </td>
                <td title={a.fills ? `${a.liquidity || "?"} · fees ${money(a.fees)} · ${a.fills} fill${a.fills > 1 ? "s" : ""}` : a.status}>
                  <span className={`outcome ${a.result}`}>{RESULT[a.result]}</span>
                  <span className="muted cost">{resultDetail(a)}</span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
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

const OUTCOME: Record<string, string> = { won: "Won", lost: "Lost", sold: "Sold", open: "Open" };

/** What each strategy bought, what it cost, and how it ended. */
function Trades({ snap, slots }: { snap: Snapshot; slots: Record<string, number> }) {
  const [onlyOpen, setOnlyOpen] = useState(false);
  const all = snap.trades ?? [];
  const open = all.filter((t) => t.outcome === "open");
  const trades = onlyOpen ? open : all;
  if (!all.length) return <p className="empty" style={{ padding: 16 }}>No trades yet.</p>;
  const won = trades.filter((t) => t.outcome === "won").length;
  const lost = trades.filter((t) => t.outcome === "lost").length;
  const net = trades.reduce((a, t) => a + (t.pnl ?? 0), 0);
  return (
    <>
      <div className="orders-head">
        <p className="trades-summary">
          {onlyOpen
            ? <>{open.length} open · <b className="num">{money(open.reduce((a, t) => a + t.cost, 0))}</b> at cost, settling or sold later</>
            : <>{trades.length} most recent trades · <span className="up">{won} won</span> · <span className="down">{lost} lost</span>
              {" "}· net <b className={`num ${tone(net)}`}>{money(net, true)}</b></>}
        </p>
        <div className="segmented" role="radiogroup" aria-label="Which trades">
          {[false, true].map((o) => (
            <button key={String(o)} role="radio" aria-checked={onlyOpen === o} onClick={() => setOnlyOpen(o)}
              title={o ? "Positions still held: the market hasn't settled and they weren't sold" : "Every recent trade"}>
              {onlyOpen === o && <motion.span className="thumb" layoutId="trades-thumb" transition={{ type: "spring", stiffness: 500, damping: 38 }} />}
              <span>{o ? `Open ${open.length}` : "All"}</span>
            </button>
          ))}
        </div>
      </div>
      {trades.length === 0 && <p className="empty" style={{ padding: 16 }}>Nothing open right now.</p>}
      <table className="trades">
        <thead><tr><th>Opened</th><th>Strategy</th><th>Market</th><th>Bought</th>
          <th>Outcome</th><th className="r">P&L</th></tr></thead>
        <tbody>
          {trades.map((t) => (
            <tr key={`${t.instance}-${t.market_id}-${t.side}`} className={`o-${t.outcome}`}>
              <td className="muted">{clock(t.opened)}</td>
              <td><span className="who"><i className="swatch" style={{ ["--c" as string]: `var(--series-${slots[t.instance] ?? 1})` }} />{t.instance}</span></td>
              <td className="market-cell" title={t.market_id}>
                <span className="q">{nameOf(snap.names, t.market_id).title}</span>
                <span className="muted">{nameOf(snap.names, t.market_id).subtitle}</span>
              </td>
              <td>
                <span className={`side ${t.side}`}>{t.side === "yes" ? "Yes" : "No"}</span>{" "}
                <span className="num">{+t.bought.toFixed(2)} at {cents(t.avg_price)}</span>
                <span className="muted num cost" title={`includes ${money(t.fees)} in fees`}>{money(t.cost)} with fees
                  {t.sold > 0 && <> · sold {+t.sold.toFixed(2)} at {cents(t.avg_sell)}</>}</span>
              </td>
              <td><span className={`outcome ${t.outcome}`} title={t.closed ? `${t.outcome === "sold" ? "sold" : "settled"} ${clock(t.closed)}` : "the market hasn't settled yet"}>
                {OUTCOME[t.outcome]}</span></td>
              <td className={`r num pnl ${tone(t.pnl)}`}>{t.pnl == null ? "–" : money(t.pnl, true)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}

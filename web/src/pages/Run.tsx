import { motion } from "motion/react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import { historyQuery, useHistory, usePoll, useRunStream, type ActivityRow, type NewRows, type OrderStats, type Snapshot, type Trade, type TradeSummary } from "../api";
import { AnimatedNumber } from "../components/AnimatedNumber";
import { Markets } from "../components/Markets";
import { PnlChart } from "../components/PnlChart";
import { DemoClock, NewDay, SpeedToggle } from "../components/SpeedToggle";
import { Splitter, useLayout } from "../components/Splitter";
import { Strategies } from "../components/Strategies";
import { Tape } from "../components/Tape";
import { useToast } from "../components/Toasts";
import { ago, cents, clock, money, nameOf, slots as slotsOf, tone, when } from "../format";

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
  if (snap.run.name !== run) return <div className="page"><p className="empty">Loading {run}…</p></div>;
  return <Run key={`${run}:${snap.session?.speed?.seed ?? ""}`} snap={snap} live={state === "open"} />;
}

function Run({ snap, live }: { snap: Snapshot; live: boolean }) {
  const slots = useMemo(() => slotsOf(snap.strategies.map((s) => s.instance), snap.kinds), [snap.strategies, snap.kinds]);
  // which strategies the chart draws in color: everything up to 8; past that the live ones (or the
  // first 8), unless picked from the legend. Hovering a strategy anywhere highlights its line.
  const [hovered, setHovered] = useState<string | null>(null);
  const focusKey = `focus:${snap.run.name}`;
  const [picked, setPicked] = useState<Set<string> | null>(() => {
    try { const v = sessionStorage.getItem(focusKey); return v ? new Set(JSON.parse(v) as string[]) : null; } catch { return null; }
  });
  const ids = snap.strategies.map((s) => s.instance);
  const realIds = ids.filter((i) => snap.kinds?.[i] === "live");
  const focus = picked ?? (ids.length <= 8 ? new Set(ids) : new Set(realIds.length ? realIds : ids.filter((i) => slots[i] > 0)));
  const toggle = (n: string) => {
    const next = new Set(focus);
    if (next.has(n)) next.delete(n); else next.add(n);
    setPicked(next);
    try { sessionStorage.setItem(focusKey, JSON.stringify([...next])); } catch { /* private mode */ }
  };
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
            <span className={`pill ${runLabel(snap)}`}><span className="dot" />{runLabel(snap)}</span>

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
          {snap.stats.pnl_real != null ? <>
            <div className="kpi hero real" title="Realized P&L of the strategies on live brokers: real money">
              <div className="label">Real money</div>
              <AnimatedNumber className={`value ${tone(snap.stats.pnl_real)}`} value={snap.stats.pnl_real} format={(v) => money(v)} /></div>
            <div className="kpi" title="Realized P&L of the strategies on paper or shadow brokers: simulated">
              <div className="label">Simulated</div>
              <AnimatedNumber className={`value ${tone(snap.stats.pnl_simulated ?? 0)}`} value={snap.stats.pnl_simulated ?? 0} format={(v) => money(v)} /></div>
          </> : (
            <div className="kpi hero"><div className="label">Realized P&L</div>
              <AnimatedNumber className={`value ${tone(snap.stats.pnl)}`} value={snap.stats.pnl} format={(v) => money(v)} /></div>
          )}
          <div className="kpi"><div className="label">Fills</div><div className="value">{snap.stats.fills}</div></div>
          <div className="kpi"><div className="label">Open positions</div><div className="value">{open}</div></div>
          <div className="kpi"><div className="label">Errors</div>
            <div className={`value ${snap.stats.errors ? "down" : ""}`}>{snap.stats.errors}</div></div>
        </div>}
      </div>

      {loading != null ? <LoadingDay progress={loading} /> : <Workspace>{(handles) => <>
        <Strategies snap={snap} slots={slots} onHover={setHovered} focus={focus} />
        {handles.left}
        <Center snap={snap} slots={slots} halted={halted} chart={{ focus, onToggle: toggle, hovered, onHover: setHovered }} />
        {handles.right}
        <div className="right-col" ref={handles.rightCol}>
          <Markets session={snap.session} slots={slots} />
          {handles.markets}
          <section className="panel fill">
            <header><h2>Tape</h2><span className="hint">fills, settlements and alerts</span></header>
            <div className="panel-body scroll"><Tape items={snap.tape} slots={slots} names={snap.names} /></div>
          </section>
        </div>
      </>}</Workspace>}
    </div>
  );
}

const DEFAULT = { left: 280, right: 340 };
const MIN = { left: 220, right: 280, center: 380, markets: 120, tape: 140 };

/** The three columns (and the right column's two panels), with drag handles between them. */
function Workspace({ children }: {
  children: (h: { left: JSX.Element; right: JSX.Element; markets: JSX.Element;
    rightCol: React.RefObject<HTMLDivElement> }) => JSX.Element;
}) {
  const [layout, setLayout] = useLayout();
  const box = useRef<HTMLDivElement>(null);
  const rightCol = useRef<HTMLDivElement>(null);
  const left = layout.left ?? DEFAULT.left;
  const right = layout.right ?? DEFAULT.right;
  const width = () => box.current?.clientWidth ?? 1400;
  const marketsPanel = () => rightCol.current?.firstElementChild as HTMLElement | null;
  const style = {
    ["--left" as string]: `${left}px`, ["--right" as string]: `${right}px`,
    ...(layout.markets != null ? { ["--markets" as string]: `${layout.markets}px` } : {}),
  };
  return (
    <div className={`workspace ${layout.markets != null ? "sized" : ""}`} ref={box} style={style}>
      {children({
        rightCol,
        left: <Splitter axis="x" label="Strategies width" size={() => left} min={MIN.left}
          max={() => width() - right - MIN.center - 24} onResize={(px) => setLayout({ left: px })}
          onReset={() => setLayout({ left: undefined })} />,
        right: <Splitter axis="x" sign={-1} label="Markets and tape width" size={() => right} min={MIN.right}
          max={() => width() - left - MIN.center - 24} onResize={(px) => setLayout({ right: px })}
          onReset={() => setLayout({ right: undefined })} />,
        markets: <Splitter axis="y" label="Markets height" size={() => marketsPanel()?.offsetHeight ?? 300}
          min={MIN.markets} max={() => (rightCol.current?.clientHeight ?? 800) - MIN.tape - 12}
          onResize={(px) => setLayout({ markets: px })} onReset={() => setLayout({ markets: undefined })} />,
      })}
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

const hasTwins = (snap: Snapshot) => Object.keys(snap.twins ?? {}).length > 0;

/** "live" when the run has real money, else "paper" (or its mode, for a backtest). */
export const runLabel = (snap: Snapshot): string =>
  snap.run.mode === "backtest" ? "backtest" : snap.real || snap.run.mode === "live" ? "live" : "paper";

const TABS = ["chart", "trades", "orders", "commands", "compare"] as const;
type Tab = (typeof TABS)[number];

const RANGES: Record<string, number | null> = { "1h": 3_600_000, "6h": 21_600_000, "24h": 86_400_000, All: null };

/** The center panel: the P&L chart, or one of the tables, in the same space. */
function Center({ snap, slots, halted, chart }: {
  snap: Snapshot; slots: Record<string, number>; halted: Set<string>;
  chart: { focus: Set<string>; onToggle: (n: string) => void; hovered: string | null; onHover: (n: string | null) => void };
}) {
  const [tab, setTab] = useState<Tab>(() => {
    try {
      const t = sessionStorage.getItem("tab:run") as Tab;
      return TABS.includes(t) && (t !== "compare" || hasTwins(snap)) ? t : "chart";
    } catch { return "chart"; }
  });
  const pick = (t: Tab) => { setTab(t); try { sessionStorage.setItem("tab:run", t); } catch { /* private mode */ } };
  // Compare only when a live strategy has a paper twin to compare with
  const tabs = TABS.filter((t) => t !== "compare" || hasTwins(snap));
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.target instanceof HTMLInputElement || e.target instanceof HTMLSelectElement || e.metaKey || e.ctrlKey || e.altKey) return;
      const i = ["1", "2", "3", "4", "5"].indexOf(e.key);
      if (i >= 0 && i < tabs.length) pick(tabs[i]);
    };
    addEventListener("keydown", onKey);
    return () => removeEventListener("keydown", onKey);
  }, [tabs.length]);
  useEffect(() => { if (!tabs.includes(tab)) pick("chart"); }, [tabs.length]);
  const counts: Record<Tab, number | null> = {
    chart: null, trades: snap.trade_total ?? snap.trades?.length ?? 0, orders: needsLook(snap.order_stats) ?? snap.activity.length,
    commands: snap.commands.length, compare: null,
  };
  const label = (t: Tab) => (t === "chart" ? "P&L" : t[0].toUpperCase() + t.slice(1));
  const [range, setRange] = useState<string>(() => {
    try { const r = localStorage.getItem("chart:range"); return r && r in RANGES ? r : "All"; } catch { return "All"; }
  });
  const pickRange = (r: string) => { setRange(r); try { localStorage.setItem("chart:range", r); } catch { /* private mode */ } };
  return (
    <section className="panel center">
      <div className="tabbar" role="tablist">
        {tabs.map((t, i) => (
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
        {tab === "chart" && <PnlChart curves={snap.curves} slots={slots} halted={halted} range={RANGES[range]} kinds={snap.kinds} {...chart}
          now={snap.session?.now ?? null} />}
        {tab === "trades" && <Trades snap={snap} slots={slots} />}
        {tab === "orders" && <Orders snap={snap} slots={slots} />}
        {tab === "commands" && <Commands snap={snap} />}
        {tab === "compare" && hasTwins(snap) && <Compare snap={snap} />}
      </div>
    </section>
  );
}

/** Orders that didn't simply fill, over every order (null before the ledger reported counts). */
const needsLook = (stats: OrderStats | undefined): number | null =>
  stats ? Object.values(stats).reduce((n, s) => n + (s.needs_look ?? s.orders - s.filled), 0) : null;

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

const RANGE_MS: Record<string, number | undefined> = { "All time": undefined, "Last hour": 3_600_000, "Last 6 hours": 21_600_000, "Last day": 86_400_000 };
const PAGE = 200;

/** Strategy, a view, a time range and a CSV download: the controls above Trades and Orders. */
function HistoryBar({ snap, instance, setInstance, money: kind, setMoney, view, setView, views, range, setRange, csv, children }: {
  snap: Snapshot; instance: string; setInstance: (v: string) => void; money: string; setMoney: (v: string) => void;
  view: string; setView: (v: string) => void;
  views: [string, string][]; range: string; setRange: (v: string) => void; csv: string; children: React.ReactNode;
}) {
  return (
    <div className="history-bar">
      <p className="trades-summary">{children}</p>
      <div className="filters">
        <select value={instance} onChange={(e) => setInstance(e.target.value)} aria-label="Strategy">
          <option value="">All strategies</option>
          {snap.strategies.map((st) => <option key={st.instance} value={st.instance}>{st.instance}</option>)}
        </select>
        {mixed(snap) && (
          <select value={kind} onChange={(e) => setMoney(e.target.value)} aria-label="Money">
            <option value="">Real and simulated</option><option value="real">Real money</option><option value="simulated">Simulated</option>
          </select>
        )}
        <select value={view} onChange={(e) => setView(e.target.value)} aria-label="Show">
          {views.map(([v, label]) => <option key={v} value={v}>{label}</option>)}
        </select>
        <select value={range} onChange={(e) => setRange(e.target.value)} aria-label="Time range">
          {Object.keys(RANGE_MS).map((r) => <option key={r}>{r}</option>)}
        </select>
        <a className="btn" href={csv} download title="Everything these filters select, as a spreadsheet (times in UTC)">Download CSV</a>
      </div>
    </div>
  );
}

/** Whether this run has both real-money and simulated strategies. */
const mixed = (snap: Snapshot) => {
  const ks = Object.values(snap.kinds ?? {});
  return ks.includes("live") && ks.some((k) => k !== "live");
};

/** Filters kept per browser, and the time they start from (the run's own clock, so a demo's too). */
function useFilters(key: string, snap: Snapshot, defaultView: string) {
  const load = () => { try { return JSON.parse(localStorage.getItem(key) ?? "{}"); } catch { return {}; } };
  const [f, setF] = useState<{ instance: string; money: string; view: string; range: string }>(() => ({ instance: "", money: "", view: defaultView, range: "All time", ...load() }));
  const set = (patch: Partial<typeof f>) => setF((cur) => {
    const next = { ...cur, ...patch };
    try { localStorage.setItem(key, JSON.stringify(next)); } catch { /* private mode */ }
    return next;
  });
  const now = snap.session?.now ?? snap.run.last_ts ?? Date.now();
  const span = RANGE_MS[f.range];
  const since = span ? Math.floor((now - span) / 60_000) * 60_000 : undefined; // to the minute: fewer refetches
  return { f, set, since };
}

function LoadMore({ shown, total, onMore, loading }: { shown: number; total: number; onMore: () => void; loading: boolean }) {
  if (!total) return null;
  return (
    <div className="load-more">
      <span className="muted">Showing {shown.toLocaleString()} of {total.toLocaleString()}</span>
      {shown < total && <button className="btn" onClick={onMore} disabled={loading}>
        {loading ? "Loading…" : `Load ${Math.min(PAGE, total - shown)} more`}</button>}
    </div>
  );
}

const ORDER_VIEWS: [string, string][] = [
  ["look", "Needs a look"], ["", "All orders"], ["stops", "Stop losses"], ["missed", "Missed"], ["partial", "Partly filled"],
  ["rejected", "Rejected"], ["resting", "Resting"], ["unfilled", "Not filled"], ["filled", "Filled"],
];

/** Execution: what each order asked for and how it turned out, over the run's whole history. By
 * default only the orders worth a look (missed, partly filled, rejected, resting, stop losses). */
function Orders({ snap, slots }: { snap: Snapshot; slots: Record<string, number> }) {
  const { f, set, since } = useFilters("history:orders", snap, "look");
  const [limit, setLimit] = useState(PAGE);
  useEffect(() => setLimit(PAGE), [f.instance, f.money, f.view, f.range]);
  const params = { instance: f.instance, money: mixed(snap) ? f.money : "", view: f.view, since };
  const { data, loading } = useHistory<{ rows: ActivityRow[]; stats: OrderStats; names: Snapshot["names"] }>(
    snap.run.name, "orders", params, limit, `${snap.stats.fills}:${snap.run.last_ts}:${snap.order_stats ? needsLook(snap.order_stats) : ""}`);
  const rows = data?.rows ?? [];
  const names = { ...snap.names, ...data?.names };
  const total: Record<string, number> = {};
  for (const st of Object.values(data?.stats ?? {})) for (const [k, v] of Object.entries(st)) total[k] = (total[k] ?? 0) + (v ?? 0);
  const matching = f.view === "" ? total.orders ?? 0 : f.view === "look" ? total.needs_look ?? 0 : total[f.view] ?? 0;
  const parts = (["missed", "partial", "unfilled", "rejected", "resting"] as const)
    .filter((k) => total[k]).map((k) => `${total[k]} ${RESULT[k].toLowerCase()}`);
  if (total.stops) parts.push(`${total.stops} stop loss${total.stops > 1 ? "es" : ""}`);
  const ref = snap.session?.now ?? snap.run.last_ts ?? Date.now();
  const csv = `/api/runs/${snap.run.name}/orders?${historyQuery({ ...params, format: "csv" })}`;
  return (
    <>
      <HistoryBar snap={snap} instance={f.instance} setInstance={(v) => set({ instance: v })} money={f.money} setMoney={(v) => set({ money: v })} view={f.view}
        setView={(v) => set({ view: v })} views={ORDER_VIEWS} range={f.range} setRange={(v) => set({ range: v })} csv={csv}>
        {(total.orders ?? 0).toLocaleString()} orders · <b>{total.orders ? Math.round((100 * (total.filled ?? 0)) / total.orders) : 0}%</b> filled in full
        {parts.length > 0 && <> · {parts.join(" · ")}</>}
      </HistoryBar>
      {!loading && rows.length === 0 ? (
        <p className="empty" style={{ padding: 16 }}>
          {f.view === "look" && total.orders ? "Every order filled in full. What they bought is in Trades." : "No orders match."}</p>
      ) : (
        <table className="trades orders">
          <thead><tr><th>Sent</th><th>Strategy</th><th>Market</th><th>Order</th><th>Result</th></tr></thead>
          <tbody>
            {rows.map((a) => (
              <tr key={a.id} className={`r-${a.result}`}>
                <td className="muted nowrap">{when(a.created_ts, ref)}</td>
                <td><span className="who"><i className="swatch" style={{ ["--c" as string]: `var(--series-${slots[a.instance] ?? 1})` }} />{a.instance}</span></td>
                <td className="market-cell" title={a.market_id}>
                  <span className="q">{nameOf(names, a.market_id).title}</span>
                  <span className="muted">{nameOf(names, a.market_id).subtitle}</span>
                </td>
                <td>
                  {a.action === "buy" ? "Buy" : "Sell"} <span className="num">{qty(a.qty)}</span>{" "}
                  <span className={`side ${a.side}`}>{a.side === "yes" ? "Yes" : "No"}</span>{" "}
                  <span className="num">at {cents(a.price)}</span>
                  <span className="muted cost">{a.action === "sell" && a.tag === "stop" && <span className="stop-tag">Stop loss</span>}
                    {[a.tif.toUpperCase(), a.post_only && "post-only", a.tag !== "stop" && a.tag].filter(Boolean).join(" · ")}</span>
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
      <LoadMore shown={rows.length} total={matching} loading={loading} onMore={() => setLimit((l) => l + PAGE)} />
    </>
  );
}

interface Side {
  orders: number; filled_pct: number | null; missed_pct: number | null; rejected: number; trades: number;
  win_rate: number | null; stops: number; pnl: number; pnl_per_trade: number | null;
  avg_paid: number | null; vs_limit: number | null; fees_per_contract: number | null; contracts: number;
}
interface Comparison { pairs: Record<string, { twin: string; since: number | null; live: Side; paper: Side }> }

const pctv = (v: number | null) => (v == null ? "–" : `${Math.round(v * 100)}%`);
const signedCents = (v: number | null) => (v == null ? "–" : `${v > 0 ? "+" : v < 0 ? "−" : ""}${cents(Math.abs(v))}`);
const METRICS: { key: keyof Side; label: string; hint: string; fmt: (v: number | null) => string; diff?: "pct" | "cents" | "money" | "count" }[] = [
  { key: "orders", label: "Orders", hint: "Orders sent", fmt: (v) => String(v ?? 0), diff: "count" },
  { key: "filled_pct", label: "Filled in full", hint: "Share of orders that filled completely", fmt: pctv, diff: "pct" },
  { key: "missed_pct", label: "Missed", hint: "Share of orders (IOC) that found nothing at their price", fmt: pctv, diff: "pct" },
  { key: "rejected", label: "Rejected", hint: "Orders refused, by quanty's checks or the venue", fmt: (v) => String(v ?? 0), diff: "count" },
  { key: "vs_limit", label: "Fills vs limit", hint: "Per contract bought: the price paid minus the limit asked (negative: better than asked)", fmt: signedCents, diff: "cents" },
  { key: "avg_paid", label: "Avg price paid", hint: "Per contract bought", fmt: (v) => (v == null ? "–" : cents(v)), diff: "cents" },
  { key: "fees_per_contract", label: "Fees per contract", hint: "Fees on buys, per contract", fmt: (v) => (v == null ? "–" : cents(v)), diff: "cents" },
  { key: "trades", label: "Trades", hint: "Positions taken", fmt: (v) => String(v ?? 0), diff: "count" },
  { key: "win_rate", label: "Win rate", hint: "Of the positions that settled", fmt: pctv, diff: "pct" },
  { key: "stops", label: "Stopped out", hint: "Positions sold by a stop loss", fmt: (v) => String(v ?? 0), diff: "count" },
  { key: "pnl_per_trade", label: "P&L per trade", hint: "Realized, after fees, per position closed", fmt: (v) => (v == null ? "–" : money(v, true)), diff: "money" },
  { key: "pnl", label: "P&L", hint: "Realized, after fees", fmt: (v) => money(v ?? 0, true), diff: "money" },
];

function delta(m: (typeof METRICS)[number], a: number | null, b: number | null): string {
  if (a == null || b == null || !m.diff) return "";
  const d = b - a;
  if (Math.abs(d) < 1e-9) return "same";
  const sign = d > 0 ? "+" : "−";
  if (m.diff === "pct") return `${sign}${Math.abs(Math.round(d * 100))} pts`;
  if (m.diff === "cents") return `${sign}${cents(Math.abs(d))}`;
  if (m.diff === "money") return `${sign}${money(Math.abs(d))}`;
  return `${sign}${Math.abs(d)}`;
}

/** Each live strategy beside its paper twin, in the same session: the same markets at the same
 * moments, real fills against simulated ones. */
function Compare({ snap }: { snap: Snapshot }) {
  const { data, error } = usePoll<Comparison>(`/api/runs/${snap.run.name}/compare`, 5000);
  if (error) return <p className="empty" style={{ padding: 16 }}>Couldn't compare: {error}</p>;
  if (!data) return <p className="empty" style={{ padding: 16 }}>Loading…</p>;
  const ref = snap.session?.now ?? snap.run.last_ts ?? Date.now();
  return (
    <>
      <p className="trades-summary compare-note">
        Each live strategy beside its paper twin: the same strategy and params on a simulated broker, in the same
        session, so both saw the same markets at the same moments. Differences in fill rate and fills vs limit show
        how far paper's simulated fills are from the venue's.
      </p>
      {Object.entries(data.pairs).map(([iid, { twin, since, live, paper }]) => (
        <table key={iid} className="compare">
          <thead><tr><th>{iid} <span className="muted small">vs {twin}{since != null && <>, since {when(since, ref)}</>}</span></th>
            <th className="r">Paper twin</th><th className="r">Live</th><th className="r">Live − paper</th></tr></thead>
          <tbody>
            {METRICS.map((m) => (
              <tr key={m.key}>
                <td title={m.hint}>{m.label}</td>
                <td className="r num">{m.fmt(paper[m.key] as number | null)}</td>
                <td className="r num">{m.fmt(live[m.key] as number | null)}</td>
                <td className="r num muted">{delta(m, paper[m.key] as number | null, live[m.key] as number | null)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ))}
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

const OUTCOME: Record<string, string> = { won: "Won", lost: "Lost", sold: "Sold", stopped: "Stopped", open: "Open" };

const TRADE_VIEWS: [string, string][] = [
  ["", "All trades"], ["open", "Open"], ["won", "Won"], ["lost", "Lost"], ["stopped", "Stopped"], ["sold", "Sold"],
];

/** What each strategy bought, what it cost, and how it ended, over the run's whole history. */
function Trades({ snap, slots }: { snap: Snapshot; slots: Record<string, number> }) {
  const { f, set, since } = useFilters("history:trades", snap, "");
  const [limit, setLimit] = useState(PAGE);
  useEffect(() => setLimit(PAGE), [f.instance, f.money, f.view, f.range]);
  const params = { instance: f.instance, money: mixed(snap) ? f.money : "", outcome: f.view, since };
  const { data, loading } = useHistory<{ rows: Trade[]; summary: TradeSummary; names: Snapshot["names"] }>(
    snap.run.name, "trades", params, limit, `${snap.stats.fills}:${snap.run.last_ts}`);
  const rows = data?.rows ?? [];
  const names = { ...snap.names, ...data?.names };
  const sum = data?.summary;
  const ref = snap.session?.now ?? snap.run.last_ts ?? Date.now();
  const csv = `/api/runs/${snap.run.name}/trades?${historyQuery({ ...params, format: "csv" })}`;
  return (
    <>
      <HistoryBar snap={snap} instance={f.instance} setInstance={(v) => set({ instance: v })} money={f.money} setMoney={(v) => set({ money: v })} view={f.view}
        setView={(v) => set({ view: v })} views={TRADE_VIEWS} range={f.range} setRange={(v) => set({ range: v })} csv={csv}>
        {sum ? <>
          {sum.trades.toLocaleString()} trades · <span className="up">{sum.won} won</span> · <span className="down">{sum.lost} lost</span>
          {sum.stopped > 0 && <> · <span className="stopped">{sum.stopped} stopped</span></>}
          {sum.open > 0 && <> · {sum.open} open</>}
          {" "}· net <b className={`num ${tone(sum.pnl)}`}>{money(sum.pnl, true)}</b>
        </> : "Loading…"}
      </HistoryBar>
      {!loading && rows.length === 0 ? (
        <p className="empty" style={{ padding: 16 }}>{f.view === "open" ? "Nothing open right now." : "No trades match."}</p>
      ) : (
        <table className="trades">
          <thead><tr><th>Opened</th><th>Strategy</th><th>Market</th><th>Bought</th>
            <th>Outcome</th><th className="r">P&L</th></tr></thead>
          <tbody>
            {rows.map((t) => (
              <tr key={`${t.instance}-${t.market_id}-${t.side}`} className={`o-${t.outcome}`}>
                <td className="muted nowrap">{when(t.opened, ref)}</td>
                <td><span className="who"><i className="swatch" style={{ ["--c" as string]: `var(--series-${slots[t.instance] ?? 1})` }} />{t.instance}</span></td>
                <td className="market-cell" title={t.market_id}>
                  <span className="q">{nameOf(names, t.market_id).title}</span>
                  <span className="muted">{nameOf(names, t.market_id).subtitle}</span>
                </td>
                <td>
                  <span className={`side ${t.side}`}>{t.side === "yes" ? "Yes" : "No"}</span>{" "}
                  <span className="num">{+t.bought.toFixed(2)} at {cents(t.avg_price)}</span>
                  <span className="muted num cost" title={`includes ${money(t.fees)} in fees`}>{money(t.cost)} with fees
                    {t.sold > 0 && <> · {t.stopped ? "stopped out" : "sold"} {+t.sold.toFixed(2)} at {cents(t.avg_sell)}</>}</span>
                </td>
                <td><span className={`outcome ${t.outcome}`} title={t.closed ? `${t.outcome === "stopped" ? "sold by its stop loss" : t.outcome === "sold" ? "sold" : "settled"} ${clock(t.closed)}` : "the market hasn't settled yet"}>
                  {OUTCOME[t.outcome]}</span></td>
                <td className={`r num pnl ${tone(t.pnl)}`}>{t.pnl == null ? "–" : money(t.pnl, true)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <LoadMore shown={rows.length} total={sum ? (f.view ? sum[f.view as keyof TradeSummary] as number : sum.trades) : 0}
        loading={loading} onMore={() => setLimit((l) => l + PAGE)} />
    </>
  );
}

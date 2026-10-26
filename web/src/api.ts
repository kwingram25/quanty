import { useEffect, useRef, useState } from "react";

/** session: an experiment's paper and live sessions in one run; live and paper: older runs, one per mode */
export type Mode = "session" | "live" | "paper" | "backtest";

export interface RunRow {
  name: string;
  mode: Mode;
  pnl: number;
  fills: number;
  errors: number;
  strategies: number;
  last_ts: number | null;
  running: boolean;
  /** live (has real money), paper or backtest; with pnl_real / pnl_simulated apart when live */
  label?: string;
  pnl_real?: number;
  pnl_simulated?: number;
}

export interface Strategy {
  instance: string;
  orders: number;
  rejected: number;
  fills: number;
  contracts: number;
  fees: number;
  pnl: number;
  wins: number;
  losses: number;
  info?: StrategyInfo;
}

/** What an instance is, recorded when its run started (older ledgers don't have it). */
export interface StrategyInfo {
  strategy?: string;
  description?: string;
  about?: string;
  series?: string[];
  params?: Record<string, unknown>;
  params_set?: string[];
  param_help?: Record<string, string>;
  broker?: string;
  limits?: Record<string, number>;
  markets_traded?: number;
  /** params changed from the dashboard (differing from the experiment file) */
  edited?: string[];
}

export interface Held { instance: string; side: string; qty: number; avg: number }

export interface OpenMarket {
  id: string;
  series: string;
  title: string;
  subtitle: string;
  event: string;
  strike: number | null;
  reference: string | null;
  reference_value: number | null;
  unit: string;
  open_ts: number;
  close_ts: number;
  bid: number | null;
  ask: number | null;
  held: Held[];
  /** each strategy's own probability of YES (`ctx.estimate`), where it reports one */
  estimates?: Record<string, number>;
}

export interface Broker {
  kind: "sim" | "shadow" | "live"; armed?: boolean; disabled?: string;
  /** a live broker's max_qty and max_notional are both set (without them it places nothing) */
  capped?: boolean;
  /** a shadow broker: venues whose keys aren't configured (it can't check their orders) */
  keys_missing?: string[];
}

export interface SessionState {
  /** commands this session's quanty understands (missing: a session from before they were listed) */
  actions?: string[];
  /** how dashboard edits reach the experiment file: written by the session, or copied by you (a
   *  session in a container image, whose copy a redeploy replaces); null in a demo */
  save?: "file" | "copy" | null;
  /** whether the session can reload its experiment file (start, retire and update instances) */
  reload?: boolean;
  pid: number;
  heartbeat_ts: number;
  can_arm: boolean;
  instances: Record<string, {
    halted: boolean; broker: string; blocked?: number;
    /** removed from the experiment and reloaded: no new entries, what it holds finishes */
    retired?: boolean;
    /** switched on this server and kept with the run, not yet in the experiment file */
    switched?: { broker: string; kind: string; twin: string | null } | null;
    /** current params, and the names changed from the dashboard */
    params?: Record<string, unknown>; edited?: string[];
  }>;
  brokers: Record<string, Broker>;
  now: number | null;
  markets: OpenMarket[];
  /** exclusive event -> instance -> worst-case loss and best-case gain */
  exposure?: Record<string, Record<string, { loss: number; gain: number }>>;
  /** a demo's playback speed (x real time) and the speeds it can switch to */
  speed?: { value: number; options: number[]; catching_up?: boolean; progress?: number; seed?: number | null; new_day?: boolean } | null;
  /** feeds this session is connected to and their health ("simulated" in a demo) */
  platforms?: { name: string; status: string }[];
}

export interface ActivityRow {
  id: string;
  instance: string;
  market_id: string;
  side: string;
  action: string;
  price: number;
  qty: number;
  tif: string;
  post_only: boolean;
  tag: string;
  created_ts: number;
  status: string;
  filled: number;
  reason: string;
  fills: number;
  avg_price: number | null;
  fees: number;
  realized: number;
  liquidity: string;
  /** how it turned out: filled, partial, missed (IOC found nothing), unfilled, rejected, resting */
  result: "filled" | "partial" | "missed" | "unfilled" | "rejected" | "resting";
}

/** Per strategy, over every order: how many there were and how they turned out. */
export type OrderStats = Record<string, Record<"orders" | ActivityRow["result"], number> & {
  /** what the Orders view shows by default; stop-loss sells that filled */
  needs_look?: number; stops?: number;
}>;

export interface Position { instance: string; market_id: string; side: string; qty: number; avg_price: number }

export type TapeItem =
  | { key: string; kind: "fill"; ts: number; instance: string; market_id: string; side: string; action: string;
      price: number; qty: number; fee: number; realized: number; liquidity: string; tag?: string }
  | { key: string; kind: "settlement"; ts: number; instance: string; market_id: string; side: string; qty: number;
      payout: number; pnl: number }
  | { key: string; kind: "log"; ts: number; instance: string; level: string; msg: string;
      fields: Record<string, unknown> };

export interface CommandRow {
  id: number; action: string; target: string; created_ts: number; status: string; result: string;
}

export interface Snapshot {
  run: { name: string; mode: Mode; file: string; last_ts: number | null };
  /** pnl_real / pnl_simulated: only when some instances trade real money (never one total then) */
  stats: { pnl: number; fills: number; errors: number; pnl_real?: number; pnl_simulated?: number };
  /** each instance's broker kind: live = real money, shadow, sim */
  kinds?: Record<string, "live" | "shadow" | "sim" | string>;
  strategies: Strategy[];
  curves: Record<string, [number, number][]>;
  positions: Position[];
  activity: ActivityRow[];
  /** the latest orders that didn't simply fill in full */
  order_issues?: ActivityRow[];
  order_stats?: OrderStats;
  tape: TapeItem[];
  session: SessionState | null;
  commands: CommandRow[];
  names: Record<string, MarketName>;
  trades: Trade[];
  /** positions over the whole run */
  trade_total?: number;
  /** live instance -> its paper twin (`quanty promote` keeps one), for the Compare tab */
  twins?: Record<string, string>;
  /** whether the run has real money (its live strategies, or real orders in its history) */
  real?: boolean;
  /** whether actions on it need the run's name typed (it has real money, or a live broker) */
  needs_confirm?: boolean;
  /** realized P&L per instance and kind, from what each fill and settlement recorded */
  pnl_kinds?: Record<string, Record<string, number>>;
}

/** One position a strategy took: what it bought, what it cost and how it ended. */
export interface Trade {
  instance: string;
  market_id: string;
  side: string;
  opened: number;
  closed: number | null;
  bought: number;
  avg_price: number | null;
  cost: number;
  sold: number;
  avg_sell: number | null;
  fees: number;
  /** stopped: sold by a stop loss (an exit the strategy tagged "stop") */
  outcome: "won" | "lost" | "sold" | "stopped" | "open";
  stopped?: boolean;
  pnl: number | null;
}

export interface MarketName { title: string; subtitle: string; event: string; series: string }

export interface Account {
  venue: string;
  label: string;
  cash: number | null;
  buying_power: number | null;
  positions_value: number | null;
  updated: number | null;
  error: string | null;
  busy: boolean;
}

export interface FeedRow {
  feed: string;
  status: string;
  latest: { messages: number; lag_p50: number | null; lag_p99: number | null; gaps: number; parse_errors: number;
            reconnects: number; end: number };
  lag_p99: [number, number][];
}

const token = () => document.querySelector<HTMLMetaElement>('meta[name="quanty-token"]')?.content ?? "";

/** The session ended (the dashboard has a login): back to the sign-in page, then here again. */
export const toLogin = () => location.assign(`/login?next=${encodeURIComponent(location.pathname)}`);

export async function getJSON<T>(url: string): Promise<T> {
  const r = await fetch(url);
  if (r.status === 401) toLogin();
  if (!r.ok) {
    const text = await r.text();
    let detail = text;
    try { detail = JSON.parse(text).detail ?? text; } catch { /* not JSON */ }
    throw new Error(`${r.status}: ${detail}`);
  }
  return r.json() as Promise<T>;
}

export async function sendCommand(run: string, action: string, target = "", confirm = "",
    value?: Record<string, unknown>): Promise<{ ok: boolean; result: string }> {
  const r = await fetch(`/api/runs/${run}/control`, {
    method: "POST",
    headers: { "content-type": "application/json", "x-quanty-token": token() },
    body: JSON.stringify({ action, target, confirm, value }),
  });
  if (r.status === 401) toLogin();
  const body = await r.json().catch(() => ({}));
  if (!r.ok) return { ok: false, result: body.detail ?? `HTTP ${r.status}` };
  return body;
}

/** A backtest the dashboard started (one at a time): running, done (its run) or failed (why). */
export interface BacktestJob {
  state: "running" | "done" | "failed";
  run: string; instance: string; result: string; variants: number; hours: number;
  started: number; finished: number | null; error: string | null;
}

async function post<T>(url: string, body?: unknown): Promise<T> {
  const r = await fetch(url, {
    method: "POST",
    headers: { "content-type": "application/json", "x-quanty-token": token() },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (r.status === 401) toLogin();
  const out = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(out.detail ?? `HTTP ${r.status}`);
  return out as T;
}

export const startBacktest = (run: string, instance: string, grid: Record<string, unknown[]>, hours: number) =>
  post<BacktestJob>(`/api/runs/${run}/backtest`, { instance, grid, hours });
export const stopBacktest = () => post<{ stopped: boolean }>("/api/backtest/stop");

/** One page of a run's history (trades or orders), filtered on the server. Refetched when the
 *  filters or the page size change, and when `tick` changes (new data), at most every 2 s. */
export function useHistory<T>(run: string, what: "trades" | "orders", params: Record<string, string | number | undefined>,
    limit: number, tick: unknown): { data: T | null; loading: boolean; error: string | null } {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const last = useRef(0);
  const qs = historyQuery(params);
  useEffect(() => { last.current = 0; setLoading(true); }, [run, what, qs]); // new filters: fetch at once
  useEffect(() => {
    let alive = true;
    const wait = Math.max(0, last.current + 2000 - Date.now());
    const t = setTimeout(() => {
      last.current = Date.now();
      getJSON<T>(`/api/runs/${run}/${what}?${qs}&limit=${limit}`)
        .then((d) => { if (alive) { setData(d); setError(null); setLoading(false); } })
        .catch((e) => { if (alive) { setError(String(e)); setLoading(false); } });
    }, wait);
    return () => { alive = false; clearTimeout(t); };
  }, [run, what, qs, limit, tick]);
  return { data, loading, error };
}

export const historyQuery = (params: Record<string, string | number | undefined>): string =>
  new URLSearchParams(Object.entries(params).filter(([, v]) => v !== undefined && v !== "")
    .map(([k, v]) => [k, String(v)])).toString();

export interface TradeSummary { trades: number; won: number; lost: number; stopped: number; sold: number; open: number; pnl: number; cost: number }

/** Polls a JSON endpoint. */
export function usePoll<T>(url: string, everyMs = 3000): { data: T | null; error: string | null } {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    const tick = () => getJSON<T>(url).then((d) => alive && (setData(d), setError(null)))
      .catch((e) => alive && setError(String(e)));
    tick();
    const id = setInterval(tick, everyMs);
    return () => { alive = false; clearInterval(id); };
  }, [url, everyMs]);
  return { data, error };
}

export interface NewRows { fills: unknown[]; settlements: unknown[]; logs: unknown[] }

/** A run's snapshot, pushed over a WebSocket as it changes (reconnecting); `onNew` gets the rows
 *  that arrived since the previous push, for callouts. */
export function useRunStream(run: string, onNew?: (n: NewRows, snap: Snapshot) => void) {
  const [snap, setSnap] = useState<Snapshot | null>(null);
  const [state, setState] = useState<"connecting" | "open" | "closed" | "missing">("connecting");
  const onNewRef = useRef(onNew);
  onNewRef.current = onNew;
  useEffect(() => {
    let ws: WebSocket | null = null;
    let retry: ReturnType<typeof setTimeout> | undefined;
    let alive = true;
    setSnap(null); // another run: never show the previous one's numbers under this one's name
    setState("connecting");
    getJSON<Snapshot>(`/api/runs/${run}`).then((s) => alive && setSnap(s))
      .catch(() => alive && setState("missing"));
    const connect = () => {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      ws = new WebSocket(`${proto}://${location.host}/api/runs/${run}/stream`);
      ws.onopen = () => setState("open");
      ws.onmessage = (e) => {
        const msg = JSON.parse(e.data);
        if (msg.type === "snapshot") {
          setSnap(msg.data);
          if (msg.new) onNewRef.current?.(msg.new, msg.data);
        }
      };
      ws.onclose = (e) => {
        if (!alive) return;
        if (e.code === 1008) { toLogin(); return; } // not signed in
        setState("closed");
        retry = setTimeout(connect, 2000);
      };
    };
    connect();
    return () => { alive = false; clearTimeout(retry); ws?.close(); };
  }, [run]);
  return { snap, state };
}

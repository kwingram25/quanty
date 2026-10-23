import { useEffect, useRef, useState } from "react";

export type Mode = "live" | "paper" | "backtest";

export interface RunRow {
  name: string;
  mode: Mode;
  pnl: number;
  fills: number;
  errors: number;
  strategies: number;
  last_ts: number | null;
  running: boolean;
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
}

export interface Broker { kind: "sim" | "shadow" | "live"; armed?: boolean; disabled?: string }

export interface SessionState {
  pid: number;
  heartbeat_ts: number;
  can_arm: boolean;
  instances: Record<string, { halted: boolean; broker: string; blocked?: number }>;
  brokers: Record<string, Broker>;
  now: number | null;
  markets: OpenMarket[];
  /** exclusive event -> instance -> worst-case loss and best-case gain */
  exposure?: Record<string, Record<string, { loss: number; gain: number }>>;
  /** a demo's playback speed (x real time) and the speeds it can switch to */
  speed?: { value: number; options: number[]; catching_up?: boolean; seed?: number | null; new_day?: boolean } | null;
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
  created_ts: number;
  status: string;
  filled: number;
  reason: string;
  fills: number;
  avg_price: number | null;
  fees: number;
  realized: number;
  liquidity: string;
}

export interface Position { instance: string; market_id: string; side: string; qty: number; avg_price: number }

export type TapeItem =
  | { key: string; kind: "fill"; ts: number; instance: string; market_id: string; side: string; action: string;
      price: number; qty: number; fee: number; realized: number; liquidity: string }
  | { key: string; kind: "settlement"; ts: number; instance: string; market_id: string; side: string; qty: number;
      payout: number; pnl: number }
  | { key: string; kind: "log"; ts: number; instance: string; level: string; msg: string;
      fields: Record<string, unknown> };

export interface CommandRow {
  id: number; action: string; target: string; created_ts: number; status: string; result: string;
}

export interface Snapshot {
  run: { name: string; mode: Mode; file: string; last_ts: number | null };
  stats: { pnl: number; fills: number; errors: number };
  strategies: Strategy[];
  curves: Record<string, [number, number][]>;
  positions: Position[];
  activity: ActivityRow[];
  tape: TapeItem[];
  session: SessionState | null;
  commands: CommandRow[];
  names: Record<string, MarketName>;
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

export async function getJSON<T>(url: string): Promise<T> {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json() as Promise<T>;
}

export async function sendCommand(run: string, action: string, target = "", confirm = ""):
    Promise<{ ok: boolean; result: string }> {
  const r = await fetch(`/api/runs/${run}/control`, {
    method: "POST",
    headers: { "content-type": "application/json", "x-quanty-token": token() },
    body: JSON.stringify({ action, target, confirm }),
  });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) return { ok: false, result: body.detail ?? `HTTP ${r.status}` };
  return body;
}

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
export function useRunStream(run: string, onNew?: (n: NewRows) => void) {
  const [snap, setSnap] = useState<Snapshot | null>(null);
  const [state, setState] = useState<"connecting" | "open" | "closed" | "missing">("connecting");
  const onNewRef = useRef(onNew);
  onNewRef.current = onNew;
  useEffect(() => {
    let ws: WebSocket | null = null;
    let retry: ReturnType<typeof setTimeout> | undefined;
    let alive = true;
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
          if (msg.new) onNewRef.current?.(msg.new);
        }
      };
      ws.onclose = () => {
        if (!alive) return;
        setState("closed");
        retry = setTimeout(connect, 2000);
      };
    };
    connect();
    return () => { alive = false; clearTimeout(retry); ws?.close(); };
  }, [run]);
  return { snap, state };
}

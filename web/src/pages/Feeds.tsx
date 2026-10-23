import { usePoll, type FeedRow } from "../api";
import { ago } from "../format";

const ms = (v: number | null) => (v == null ? "–" : `${Math.round(v)} ms`);

function Spark({ pts }: { pts: [number, number][] }) {
  if (pts.length < 2) return <span className="muted">–</span>;
  const w = 120, h = 26;
  const t0 = pts[0][0], t1 = pts.at(-1)![0], hi = Math.max(...pts.map((p) => p[1])) || 1;
  const d = pts.map(([t, v], i) => `${i ? "L" : "M"}${((t - t0) / (t1 - t0 || 1)) * (w - 4) + 2},${h - 3 - (v / hi) * (h - 6)}`).join("");
  return <svg width={w} height={h} role="img" aria-label="lag p99 trend"><path d={d} fill="none" stroke="var(--series-1)" strokeWidth={1.5} /></svg>;
}

export function FeedTable() {
  const { data } = usePoll<FeedRow[]>("/api/feeds", 5000);
  if (!data) return null;
  if (!data.length) {
    return <section className="panel"><div className="panel-body"><p className="empty">No feed metrics yet:
      <code> quanty record</code> and <code>quanty paper</code> write them.</p></div></section>;
  }
  return (
    <section className="panel"><div className="table-wrap">
      <table>
        <thead><tr><th>Feed</th><th>Status</th><th className="r">Messages</th><th className="r">Lag p50</th>
          <th className="r">Lag p99</th><th>Lag p99, last 2 h</th><th className="r">Gaps</th><th className="r">Errors</th>
          <th className="r">Reconnects</th><th>Updated</th></tr></thead>
        <tbody>
          {data.map((f) => (
            <tr key={f.feed}><td>{f.feed}</td>
              <td><span className={`pill ${f.status === "ok" ? "ok" : f.status === "degraded" ? "warning" : "error"}`}><span className="dot" />{f.status}</span></td>
              <td className="r">{f.latest.messages}</td><td className="r">{ms(f.latest.lag_p50)}</td>
              <td className="r">{ms(f.latest.lag_p99)}</td><td><Spark pts={f.lag_p99} /></td>
              <td className="r">{f.latest.gaps}</td><td className="r">{f.latest.parse_errors}</td>
              <td className="r">{f.latest.reconnects}</td><td className="muted">{ago(f.latest.end)}</td></tr>
          ))}
        </tbody>
      </table>
    </div></section>
  );
}

export function FeedsPage() {
  return (
    <div className="page">
      <div className="list-head"><h1>Feeds</h1><span className="muted">health per recording window; lag is receive time minus the venue's timestamp</span></div>
      <FeedTable />
    </div>
  );
}

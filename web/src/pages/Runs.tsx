import { motion } from "motion/react";
import { Link } from "react-router-dom";
import { usePoll, type RunRow } from "../api";
import { ago, money, tone } from "../format";
import { FeedTable } from "./Feeds";

export function RunsPage() {
  const { data, error } = usePoll<RunRow[]>("/api/runs", 3000);
  return (
    <div className="page">
      <div className="list-head"><h1>Runs</h1><span className="muted">live, paper and backtest ledgers</span></div>
      {error && <p className="empty">Can't reach the server: {error}</p>}
      {data && data.length === 0 && (
        <p className="empty">No runs yet. <code>quanty demo --live</code> starts one; <code>quanty paper</code> and
          {" "}<code>quanty backtest … --ledger data/backtest/x.sqlite</code> write them here.</p>
      )}
      <div className="runs">
        {data?.map((r, i) => (
          <motion.div key={r.name} initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: i * 0.03 }}>
            <Link to={`/runs/${r.name}`} className="panel run-card">
              <div className="row">
                <h3>{r.name.split("/")[1]}</h3>
                <span style={{ display: "flex", gap: 6 }}>
                  {r.running && <span className="pill running"><span className="dot" />running</span>}
                  <span className={`pill ${r.mode}`}><span className="dot" />{r.mode}</span>
                </span>
              </div>
              <div className={`big ${tone(r.pnl)}`}>{money(r.pnl)}</div>
              <div className="meta">{r.strategies} strategies · {r.fills} fills
                {r.errors ? ` · ${r.errors} errors` : ""} · {ago(r.last_ts)}</div>
            </Link>
          </motion.div>
        ))}
      </div>
      <div className="list-head"><h1 style={{ fontSize: 18 }}>Feeds</h1></div>
      <FeedTable />
    </div>
  );
}

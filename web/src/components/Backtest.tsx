import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { getJSON, startBacktest, stopBacktest, type BacktestJob, type Snapshot } from "../api";
import { ago } from "../format";

/** "0.9, 0.95" as values of the param's type (numbers for a number, true/false for a flag). */
function parseValues(text: string, like: unknown): unknown[] {
  return text.split(",").map((t) => t.trim()).filter(Boolean).map((t) => {
    if (typeof like === "number") {
      const n = Number(t);
      if (Number.isNaN(n)) throw new Error(`${t} isn't a number`);
      return n;
    }
    if (typeof like === "boolean") {
      if (t !== "true" && t !== "false") throw new Error(`${t}: true or false`);
      return t === "true";
    }
    if (like == null) {  // unset now: a number, a flag, or text, as written
      if (t === "true" || t === "false") return t === "true";
      return Number.isNaN(Number(t)) ? t : Number(t);
    }
    return t;
  });
}

/** Backtest one of the run's strategies, as it runs now and over values of its params, on the last
 *  hours of recordings; the result is a backtest run, ranked in its own Variants tab. */
export function BacktestForm({ snap, pick }: { snap: Snapshot; pick: string }) {
  const ids = snap.strategies.map((s) => s.instance);
  const [open, setOpen] = useState(false);
  const [instance, setInstance] = useState(pick);
  const [values, setValues] = useState<Record<string, string>>({});
  const [hours, setHours] = useState("24");
  const [error, setError] = useState<string | null>(null);
  const [job, setJob] = useState<BacktestJob | null>(null);
  useEffect(() => { getJSON<BacktestJob | null>("/api/backtest").then(setJob).catch(() => {}); }, []);
  useEffect(() => {
    if (job?.state !== "running") return;
    const t = setInterval(() => getJSON<BacktestJob | null>("/api/backtest").then(setJob).catch(() => {}), 2000);
    return () => clearInterval(t);
  }, [job?.state]);
  useEffect(() => { setValues({}); setError(null); }, [instance]);
  const st = snap.strategies.find((s) => s.instance === instance);
  const params = snap.session?.instances[instance]?.params ?? st?.info?.params ?? {};
  let grid: Record<string, unknown[]> = {};
  let bad: string | null = null;
  try {
    grid = Object.fromEntries(Object.entries(values).map(([k, v]) => [k, parseValues(v, params[k])]).filter(([, v]) => v.length));
  } catch (e) { bad = (e as Error).message; }
  const combos = Object.values(grid).reduce((n, v) => n * v.length, 1);
  const tried = Object.keys(grid).length;
  const h = Number(hours);
  const running = job?.state === "running";
  const submit = () => {
    setError(null);
    startBacktest(snap.run.name, instance, grid, h).then((j) => { setJob(j); setOpen(false); }).catch((e) => setError((e as Error).message));
  };
  const mine = job && job.run === snap.run.name;
  return (
    <div className="backtest">
      {job && (mine || running) && (
        <p className={`backtest-status ${job.state}`}>
          {job.state === "running" && <>Backtesting {job.instance}: {job.variants} variants over the last {job.hours} h of
            recordings, started {ago(job.started * 1000)}. <button className="btn" onClick={() => stopBacktest().catch(() => {})}>Stop</button></>}
          {job.state === "done" && <>Backtest of {job.instance} done. <Link to={`/runs/${job.result}`}>See its {job.variants} variants ranked</Link></>}
          {job.state === "failed" && <>Backtest of {job.instance} didn't finish:<pre>{job.error}</pre></>}
        </p>
      )}
      {!open ? (
        <button className="btn" onClick={() => { setInstance(ids.includes(pick) ? pick : ids[0]); setOpen(true); }}>Backtest variants…</button>
      ) : (
        <form className="backtest-form" onSubmit={(e) => { e.preventDefault(); if (!bad && tried && !running) submit(); }}>
          <p className="muted">Replays the last hours of recordings with the strategy as it runs now and one copy per
            combination of the values below, all simulated. Nothing changes in this run; the result opens as a backtest run.</p>
          <label>Strategy <select value={instance} onChange={(e) => setInstance(e.target.value)}>
            {ids.map((i) => <option key={i} value={i}>{i}</option>)}</select></label>
          <div className="backtest-params">
            {Object.entries(params).map(([k, v]) => (
              <label key={k}><span>{k} <span className="muted">now {String(v)}</span></span>
                <input value={values[k] ?? ""} placeholder="values to try, comma-separated" aria-label={`${k}: values to try`}
                  onChange={(e) => setValues({ ...values, [k]: e.target.value })} /></label>
            ))}
            {!Object.keys(params).length && <p className="muted">This strategy's params aren't known here (its run started before quanty recorded them).</p>}
          </div>
          <label>Hours of recordings <input type="number" min="0.1" step="any" value={hours} onChange={(e) => setHours(e.target.value)} /></label>
          {(bad || error) && <p className="down">{bad ?? error}</p>}
          <div className="backtest-actions">
            <button className="btn primary" type="submit" disabled={!!bad || !tried || !(h > 0) || running}>
              {tried ? `Backtest ${combos + 1} variants` : "Backtest"}</button>
            <button className="btn" type="button" onClick={() => setOpen(false)}>Cancel</button>
            {running && <span className="muted">one at a time: wait for the running one</span>}
          </div>
        </form>
      )}
    </div>
  );
}

import { useEffect, useState } from "react";
import { getJSON } from "../api";
import { ago } from "../format";

interface Check { section: string; name: string; status: "ok" | "warn" | "fail" | "skip"; detail: string; fix: string }
interface Health { ran: number; project: string; checks: Check[] }

const MARK: Record<Check["status"], string> = { ok: "✓", warn: "!", fail: "✗", skip: "–" };

/** `quanty doctor` on the dashboard: the project's config, keys, clock against each venue, the
 *  venues themselves (keys accepted, rate-limit headroom), orders left over and disk, with fixes. */
export function HealthPage() {
  const [data, setData] = useState<Health | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const run = (fresh: boolean) => {
    setBusy(true);
    getJSON<Health>(`/api/health${fresh ? "?fresh=1" : ""}`)
      .then((d) => { setData(d); setError(null); })
      .catch((e) => setError(String(e)))
      .finally(() => setBusy(false));
  };
  useEffect(() => run(false), []);
  const sections = new Map<string, Check[]>();
  for (const c of data?.checks ?? []) sections.set(c.section, [...(sections.get(c.section) ?? []), c]);
  const fails = data?.checks.filter((c) => c.status === "fail").length ?? 0;
  const warns = data?.checks.filter((c) => c.status === "warn").length ?? 0;
  return (
    <div className="page health-page">
      <div className="list-head">
        <h1>Health</h1>
        <button className="btn" onClick={() => run(true)} disabled={busy}
          title="The venue checks make real, read-only requests (a balance, rate limits): at most once a minute">
          {busy ? "Checking…" : "Run checks again"}</button>
      </div>
      <p className="muted health-where">
        {data ? <>What <code>quanty doctor</code> checks, for the project in {data.project}. Checked {ago(data.ran * 1000)}.</> : "Checking…"}
      </p>
      {error && <p className="empty">Couldn't run the checks: {error}</p>}
      {data && (
        <p className={`health-summary ${fails ? "down" : warns ? "warn" : "up"}`}>
          {fails || warns ? `${fails} problem${fails === 1 ? "" : "s"}, ${warns} warning${warns === 1 ? "" : "s"}` : "All good"}
        </p>
      )}
      {[...sections.entries()].map(([section, checks]) => (
        <section key={section} className="panel health-section">
          <header><h2>{section}</h2></header>
          <ul>
            {checks.map((c) => (
              <li key={c.name} className={c.status}>
                <span className="mark" aria-label={c.status}>{MARK[c.status]}</span>
                <span className="what"><b>{c.name}</b> {c.detail}
                  {c.fix && <span className="fix">fix: {c.fix}</span>}</span>
              </li>
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}

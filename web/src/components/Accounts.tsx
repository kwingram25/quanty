import { usePoll, type Account } from "../api";
import { ago, money } from "../format";

/** Each venue with API keys on this machine and its balance (read-only, refreshed every 30 s). */
export function Accounts() {
  const { data } = usePoll<Account[]>("/api/accounts", 30_000);
  if (!data?.length) return null;
  return (
    <div className="accounts" aria-label="Account balances">
      {data.map((a) => {
        const detail = [
          a.buying_power != null && `buying power ${money(a.buying_power)}`,
          a.positions_value != null && `positions ${money(a.positions_value)}`,
          a.updated && `as of ${ago(a.updated)}`,
          a.error,
        ].filter(Boolean).join(" · ");
        return (
          <span key={a.venue} className={`account ${a.error ? (a.busy ? "busy" : "error") : ""}`} title={detail}>
            <span className="dot" />
            <span className="venue">{a.label}</span>
            <b className="num">{a.cash == null ? "–" : money(a.cash)}</b>
          </span>
        );
      })}
    </div>
  );
}

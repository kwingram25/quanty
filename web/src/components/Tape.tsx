import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import type { TapeItem } from "../api";
import { cents, clock, money, tone } from "../format";

/** The event tape: fills, settlements and notable events, newest first. New items slide in;
 *  wins and losses are washed green and red, warnings and errors edged amber and red. */
export function Tape({ items, slots }: { items: TapeItem[]; slots: Record<string, number> }) {
  const reduce = useReducedMotion();
  if (!items.length) return <p className="empty">Fills, settlements and alerts will appear here as they happen.</p>;
  return (
    <ul className="tape">
      <AnimatePresence initial={false}>
        {items.map((it) => (
          <motion.li key={it.key} className={cls(it)} style={edge(it, slots)} layout={!reduce}
            initial={reduce ? false : { opacity: 0, y: -10, height: 0, marginBottom: 0 }}
            animate={{ opacity: 1, y: 0, height: "auto", marginBottom: 6 }}
            exit={{ opacity: 0 }} transition={{ type: "spring", stiffness: 420, damping: 36 }}>
            <span className="when">{clock(it.ts)}</span>
            <Body it={it} />
          </motion.li>
        ))}
      </AnimatePresence>
    </ul>
  );
}

function cls(it: TapeItem): string {
  if (it.kind === "settlement") return tone(it.pnl) === "up" ? "win" : tone(it.pnl) === "down" ? "loss" : "";
  if (it.kind === "fill") return it.realized ? (it.realized > 0 ? "win" : "loss") : "";
  return it.level === "error" ? "error" : it.level === "warning" ? "warning" : "";
}

function edge(it: TapeItem, slots: Record<string, number>): React.CSSProperties | undefined {
  if (it.kind === "fill" && !it.realized) return { ["--edge" as string]: `var(--series-${slots[it.instance] ?? 1})` };
  return undefined;
}

function Body({ it }: { it: TapeItem }) {
  if (it.kind === "fill") {
    return (
      <>
        <span className="what" title={it.market_id}>
          <b>{it.instance}</b> {it.action === "buy" ? "bought" : "sold"} {it.qty} {it.side.toUpperCase()} at {cents(it.price)}
          <span className="detail">{it.liquidity} · fee {money(it.fee)}</span>
        </span>
        <span className={`amount ${tone(it.realized)}`}>{it.realized ? money(it.realized, true) : money(-it.qty * it.price - it.fee)}</span>
      </>
    );
  }
  if (it.kind === "settlement") {
    const won = it.payout > 0;
    return (
      <>
        <span className="what" title={it.market_id}>
          <b>{it.instance}</b> {won ? "won" : "lost"} {it.qty} {it.side.toUpperCase()}
          <span className="detail">{won ? `paid ${money(it.payout)}` : "expired worthless"}</span>
        </span>
        <span className={`amount ${tone(it.pnl)}`}>{money(it.pnl, true)}</span>
      </>
    );
  }
  const detail = Object.entries(it.fields).map(([k, v]) => `${k}: ${typeof v === "object" ? JSON.stringify(v) : v}`).join(" · ");
  return (
    <>
      <span className="what"><b>{it.msg}</b> <span className="muted">{it.instance}</span>
        {detail && <span className="detail">{detail}</span>}</span>
      <span className="muted">{it.level}</span>
    </>
  );
}

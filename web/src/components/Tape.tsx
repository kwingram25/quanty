import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import type { TapeItem } from "../api";
import type { MarketName } from "../api";
import { Id } from "./Id";
import { cents, clock, money, nameOf, tone } from "../format";

/** The event tape: fills, settlements and notable events, newest first. New items slide in;
 *  wins and losses are washed green and red, warnings and errors edged amber and red. */
export function Tape({ items, slots, names }: {
  items: TapeItem[]; slots: Record<string, number>; names?: Record<string, MarketName>;
}) {
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
            <Body it={it} names={names} />
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
  if (it.kind === "fill" && it.tag === "stop") return { ["--edge" as string]: "var(--serious)" };
  if (it.kind === "fill" && !it.realized) return { ["--edge" as string]: `var(--series-${slots[it.instance] ?? 1})` };
  return undefined;
}

function Body({ it, names }: { it: TapeItem; names?: Record<string, MarketName> }) {
  const what = "market_id" in it ? nameOf(names, it.market_id) : null;
  const about = what ? (what.subtitle || what.title) : "";
  if (it.kind === "fill") {
    return (
      <>
        <span className="what" title={what ? `${what.title} ${what.subtitle}`.trim() : undefined}>
          <b><Id name={it.instance} /></b> {it.action === "buy" ? "bought" : it.tag === "stop" ? "stopped out of" : "sold"} {it.qty} {it.side.toUpperCase()} at {cents(it.price)}
          <span className="detail">{about} · {it.liquidity} · fee {money(it.fee)}</span>
        </span>
        <span className={`amount ${tone(it.realized)}`}>{it.realized ? money(it.realized, true) : money(-it.qty * it.price - it.fee)}</span>
      </>
    );
  }
  if (it.kind === "settlement") {
    const won = it.payout > 0;
    return (
      <>
        <span className="what" title={what ? `${what.title} ${what.subtitle}`.trim() : undefined}>
          <b><Id name={it.instance} /></b> {won ? "won" : "lost"} {it.qty} {it.side.toUpperCase()}
          <span className="detail">{about} · {won ? `paid ${money(it.payout)}` : "expired worthless"}</span>
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

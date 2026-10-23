import { AnimatePresence, motion } from "motion/react";
import { createContext, useCallback, useContext, useRef, useState, type ReactNode } from "react";

export type ToastKind = "ok" | "info" | "warning" | "error";
interface Toast { id: number; kind: ToastKind; title: string; body?: string }

const Ctx = createContext<(t: Omit<Toast, "id">) => void>(() => {});
export const useToast = () => useContext(Ctx);

/** Toasts: errors stay until dismissed; the rest leave after a few seconds. */
export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const next = useRef(1);
  const dismiss = useCallback((id: number) => setToasts((ts) => ts.filter((t) => t.id !== id)), []);
  const push = useCallback((t: Omit<Toast, "id">) => {
    const id = next.current++;
    setToasts((ts) => [...ts.slice(-4), { ...t, id }]);
    if (t.kind !== "error") setTimeout(() => dismiss(id), t.kind === "warning" ? 8000 : 4500);
  }, [dismiss]);
  return (
    <Ctx.Provider value={push}>
      {children}
      <div className="toasts" role="status" aria-live="polite">
        <AnimatePresence initial={false}>
          {toasts.map((t) => (
            <motion.div key={t.id} className={`toast ${t.kind}`} layout
              initial={{ opacity: 0, y: 16, scale: 0.98 }} animate={{ opacity: 1, y: 0, scale: 1 }}
              exit={{ opacity: 0, x: 40 }} transition={{ type: "spring", stiffness: 420, damping: 34 }}>
              <span className="bar" />
              <div><b>{t.title}</b>{t.body && <p>{t.body}</p>}</div>
              <button aria-label="Dismiss" onClick={() => dismiss(t.id)}>✕</button>
            </motion.div>
          ))}
        </AnimatePresence>
      </div>
    </Ctx.Provider>
  );
}

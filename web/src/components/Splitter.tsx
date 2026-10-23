import { useCallback, useEffect, useRef, useState } from "react";

/** Panel sizes the viewer has dragged, in px (unset = the default layout). Remembered per browser. */
export interface Layout { left?: number; right?: number; markets?: number }

const KEY = "layout:run";

export function useLayout(): [Layout, (patch: Layout) => void] {
  const [layout, setLayout] = useState<Layout>(() => {
    try { return JSON.parse(localStorage.getItem(KEY) ?? "{}") as Layout; } catch { return {}; }
  });
  const update = useCallback((patch: Layout) => {
    setLayout((cur) => {
      const next = { ...cur, ...patch };
      for (const k of Object.keys(next) as (keyof Layout)[]) if (next[k] == null) delete next[k];
      try { localStorage.setItem(KEY, JSON.stringify(next)); } catch { /* private mode */ }
      return next;
    });
  }, []);
  return [layout, update];
}

/** A drag handle between two panels. `size` is the current size of the panel it resizes; dragging
 * by d px calls onResize(size + d * sign). Double-click resets; arrow keys nudge by 16 px. */
export function Splitter({ axis, size, sign = 1, min, max, onResize, onReset, label }: {
  axis: "x" | "y"; size: () => number; sign?: 1 | -1; min: number; max: () => number;
  onResize: (px: number) => void; onReset: () => void; label: string;
}) {
  const drag = useRef<{ start: number; from: number } | null>(null);
  const [active, setActive] = useState(false);
  const clamp = (v: number) => Math.round(Math.max(min, Math.min(max(), v)));
  useEffect(() => {
    if (!active) return;
    const prev = document.body.style.cursor;
    document.body.style.cursor = axis === "x" ? "col-resize" : "row-resize";
    document.body.classList.add("resizing"); // no text selection while dragging
    return () => { document.body.style.cursor = prev; document.body.classList.remove("resizing"); };
  }, [active, axis]);
  return (
    <div className={`splitter ${axis} ${active ? "active" : ""}`} role="separator" tabIndex={0}
      aria-orientation={axis === "x" ? "vertical" : "horizontal"} aria-label={label}
      title={`${label}: drag to resize, double-click to reset`}
      onPointerDown={(e) => {
        e.currentTarget.setPointerCapture(e.pointerId);
        drag.current = { start: axis === "x" ? e.clientX : e.clientY, from: size() };
        setActive(true);
      }}
      onPointerMove={(e) => {
        const d = drag.current;
        if (d) onResize(clamp(d.from + ((axis === "x" ? e.clientX : e.clientY) - d.start) * sign));
      }}
      onPointerUp={() => { drag.current = null; setActive(false); }}
      onPointerCancel={() => { drag.current = null; setActive(false); }}
      onDoubleClick={onReset}
      onKeyDown={(e) => {
        const step = e.shiftKey ? 64 : 16;
        const keys = axis === "x" ? { ArrowLeft: -step, ArrowRight: step } : { ArrowUp: -step, ArrowDown: step };
        const d = keys[e.key as keyof typeof keys];
        if (d) { e.preventDefault(); onResize(clamp(size() + d * sign)); }
        if (e.key === "Enter" || e.key === "Home") { e.preventDefault(); onReset(); }
      }} />
  );
}

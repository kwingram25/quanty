import { scaleLinear } from "d3-scale";
import { AnimatePresence, motion } from "motion/react";
import { useEffect, useMemo, useRef, useState } from "react";
import { clock, money, weekday, zoneName } from "../format";

type Curves = Record<string, [number, number][]>;

const PAD = { l: 64, t: 14, b: 28 };

/** The element's size, kept current as it resizes. */
function useSize<T extends HTMLElement>(): [React.RefObject<T>, { w: number; h: number }] {
  const ref = useRef<T>(null);
  const [size, setSize] = useState({ w: 760, h: 280 });
  useEffect(() => {
    if (!ref.current) return;
    const ro = new ResizeObserver(([e]) => {
      const { width, height } = e.contentRect;
      setSize({ w: Math.max(320, Math.floor(width)), h: Math.max(180, Math.floor(height)) });
    });
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);
  return [ref, size];
}

/** Cumulative realized P&L per strategy: step lines on one zero-based axis, a crosshair tooltip,
 *  and a ripple at a line's end when it moves. */
/** The curves within [now − range, now], each starting at its value just before the window (so a
 *  short range still shows cumulative P&L, not P&L since the window began). range null = all. */
function windowed(all: Curves, range: number | null, now: number | null): { curves: Curves; t0: number; t1: number } {
  const pts = Object.values(all).flat();
  const t1 = Math.max(now ?? -Infinity, ...pts.map((p) => p[0]));
  const t0 = range != null ? t1 - range : Math.min(...pts.map((p) => p[0]));
  const curves: Curves = {};
  for (const [name, series] of Object.entries(all)) {
    let before = 0;
    for (const [t, v] of series) { if (t < t0) before = v; else break; }
    curves[name] = [[t0, before], ...series.filter(([t]) => t >= t0 && t <= t1)];
  }
  return { curves, t0, t1 };
}

export function PnlChart({ curves: all, slots, halted, range = null, now = null }: {
  curves: Curves; slots: Record<string, number>; halted: Set<string>; range?: number | null; now?: number | null;
}) {
  const view = useMemo(() => windowed(all, range, now), [all, range, now]);
  const curves = view.curves;
  const names = Object.keys(curves).sort();
  const direct = names.length <= 4;
  const padR = direct ? 140 : 16;
  const [hover, setHover] = useState<{ x: number; t: number } | null>(null);
  const ref = useRef<SVGSVGElement>(null);
  const [box, { w: W, h: H }] = useSize<HTMLDivElement>();

  const { x, y, ticks, t0, t1 } = useMemo(() => {
    const pts = Object.values(curves).flat();
    let { t0, t1 } = view;
    if (t1 - t0 < 60_000) { t0 -= 30_000; t1 += 30_000; }
    const vs = pts.map((p) => p[1]);
    const y = scaleLinear().domain([Math.min(0, ...vs), Math.max(0, ...vs)]).nice(4).range([H - PAD.b, PAD.t]);
    const x = scaleLinear().domain([t0, t1]).range([PAD.l, W - padR]);
    return { x, y, ticks: y.ticks(4), t0, t1 };
  }, [curves, view, padR, W, H]);

  if (!names.length) {
    return <div className="chart-wrap" ref={box}><p className="empty">No realized P&L yet: it appears as markets settle.</p></div>;
  }

  const path = (pts: [number, number][]) =>
    `M${x(pts[0][0])},${y(pts[0][1])}` + pts.slice(1).map(([t, v]) => `H${x(t)}V${y(v)}`).join("") + `H${x(t1)}`;
  const valueAt = (pts: [number, number][], t: number) => {
    let v: number | null = null;
    for (const [pt, pv] of pts) { if (pt <= t) v = pv; else break; }
    return v;
  };
  const seconds = t1 - t0 < 600_000;

  // end labels, nudged apart
  let last = -1e9;
  const labels = names
    .map((n) => ({ n, v: curves[n].at(-1)![1] }))
    .sort((a, b) => y(a.v) - y(b.v))
    .map((l) => { const yy = Math.max(y(l.v), last + 16); last = yy; return { ...l, yy }; });

  const onMove = (e: React.PointerEvent) => {
    const box = ref.current!.getBoundingClientRect();
    const sx = ((e.clientX - box.left) / box.width) * W;
    const cx = Math.min(W - padR, Math.max(PAD.l, sx));
    setHover({ x: cx, t: x.invert(cx) });
  };

  return (
    <div className="chart-wrap">
      {names.length > 1 && (
        <div className="legend">
          {names.map((n) => (
            <span key={n} style={{ opacity: halted.has(n) ? 0.5 : 1 }}>
              <i className="swatch" style={{ ["--c" as string]: `var(--series-${slots[n] ?? 1})` }} />{n}
            </span>
          ))}
        </div>
      )}
      <div className="plot" ref={box}>
      <svg ref={ref} className="chart" width={W} height={H} viewBox={`0 0 ${W} ${H}`} role="img"
        aria-label="Cumulative realized P&L per strategy" onPointerMove={onMove} onPointerLeave={() => setHover(null)}>
        {ticks.map((v) => (
          <g key={v}>
            <line className={Math.abs(v) < 1e-9 ? "zero" : "grid"} x1={PAD.l} x2={W - padR} y1={y(v)} y2={y(v)} />
            <text className="tick" x={PAD.l - 8} y={y(v) + 4} textAnchor="end">{money(v)}</text>
          </g>
        ))}
        {[t0, (t0 + t1) / 2, t1].map((t, i) => (
          <text key={i} className="tick" x={x(t)} y={H - 8} textAnchor={i === 0 ? "start" : i === 2 ? "end" : "middle"}>
            {t1 - t0 > 20 * 3_600_000 ? `${weekday(t)} ${clock(t, false)}` : clock(t, seconds)}
          </text>
        ))}
        {names.map((n) => {
          const pts = curves[n];
          const end = pts.at(-1)!;
          return (
            <g key={n} style={{ color: `var(--series-${slots[n] ?? 1})`, opacity: halted.has(n) ? 0.45 : 1 }}>
              <path className="line" d={path(pts)} stroke="currentColor" />
              <AnimatePresence>
                <motion.circle key={`${end[0]}-${end[1]}`} cx={x(end[0])} cy={y(end[1])} fill="none"
                  stroke="currentColor" strokeWidth={2} initial={{ r: 3, opacity: 0.9 }}
                  animate={{ r: 14, opacity: 0 }} transition={{ duration: 1.1, ease: "easeOut" }} />
              </AnimatePresence>
              <circle cx={x(end[0])} cy={y(end[1])} r={3.5} fill="currentColor" />
            </g>
          );
        })}
        {direct && labels.map((l) => (
          <text key={l.n} className="end-label" x={W - padR + 10} y={l.yy + 4}>{l.n} {money(l.v)}</text>
        ))}
        {hover && <line className="cross" x1={hover.x} x2={hover.x} y1={PAD.t} y2={H - PAD.b} />}
      </svg>
      {hover && (
        <div className="tooltip" style={hover.x > W * 0.6 ? { right: W - hover.x + 12, top: 12 } : { left: hover.x + 12, top: 12 }}>
          <div className="when">{clock(hover.t)} {zoneName(hover.t)}</div>
          {names.map((n) => {
            const v = valueAt(curves[n], hover.t);
            return (
              <div key={n} className="row">
                <i className="swatch" style={{ ["--c" as string]: `var(--series-${slots[n] ?? 1})` }} />
                <span>{n}</span><span className="num">{v == null ? "–" : money(v)}</span>
              </div>
            );
          })}
        </div>
      )}
      </div>
    </div>
  );
}

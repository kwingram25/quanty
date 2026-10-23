import { animate, useReducedMotion } from "motion/react";
import { useEffect, useRef, useState } from "react";

/** A number that counts to its new value when it changes (instantly with reduced motion). */
export function AnimatedNumber({ value, format, className }: {
  value: number; format: (v: number) => string; className?: string;
}) {
  const reduce = useReducedMotion();
  const [shown, setShown] = useState(value);
  const from = useRef(value);
  useEffect(() => {
    if (reduce || from.current === value) {
      setShown(value);
      from.current = value;
      return;
    }
    const controls = animate(from.current, value, {
      duration: 0.6, ease: [0.22, 1, 0.36, 1], onUpdate: setShown,
    });
    from.current = value;
    return () => controls.stop();
  }, [value, reduce]);
  return <span className={className}>{format(shown)}</span>;
}

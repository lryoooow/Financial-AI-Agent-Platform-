import { useEffect, useRef, useState } from "react";
import { animate } from "motion/react";

interface Props {
  value: number;
  decimals?: number;
  prefix?: string;
  className?: string;
}

// 数字滚动动画 — 让行情/指标"跳动"起来
export function AnimatedNumber({ value, decimals = 2, prefix = "", className }: Props) {
  const [display, setDisplay] = useState(value);
  const prev = useRef(value);

  useEffect(() => {
    const controls = animate(prev.current, value, {
      duration: 0.6,
      ease: [0.16, 1, 0.3, 1],
      onUpdate: (v) => setDisplay(v),
    });
    prev.current = value;
    return () => controls.stop();
  }, [value]);

  return (
    <span className={`tabular ${className ?? ""}`}>
      {prefix}
      {display.toLocaleString("zh-CN", {
        minimumFractionDigits: decimals,
        maximumFractionDigits: decimals,
      })}
    </span>
  );
}

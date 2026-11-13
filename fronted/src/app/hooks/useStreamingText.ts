import { useEffect, useRef, useState } from "react";

const segmenter = new Intl.Segmenter("zh", { granularity: "grapheme" });

export function useStreamingText(text: string, streaming: boolean) {
  const [visible, setVisible] = useState(text);
  const shown = useRef(text);
  const segCache = useRef<{ text: string; segs: string[] }>({ text: "", segs: [] });

  useEffect(() => {
    if (!streaming || window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      shown.current = text;
      segCache.current = { text: "", segs: [] };
      setVisible(text);
      return;
    }
    if (!text.startsWith(shown.current)) {
      const segs = Array.from(segmenter.segment(text), ({ segment }) => segment);
      segCache.current = { text, segs };
      shown.current = text;
      setVisible(text);
      return;
    }
    let segments: string[];
    if (segCache.current.text && text.startsWith(segCache.current.text)) {
      const delta = text.slice(segCache.current.text.length);
      const extra = delta ? Array.from(segmenter.segment(delta), ({ segment }) => segment) : [];
      segments = extra.length ? [...segCache.current.segs, ...extra] : segCache.current.segs;
    } else {
      segments = Array.from(segmenter.segment(text), ({ segment }) => segment);
    }
    segCache.current = { text, segs: segments };
    let count = shown.current.length === 0
      ? 0
      : shown.current === text
        ? segments.length
        : Array.from(segmenter.segment(shown.current)).length;
    let frame = 0;
    const paint = () => {
      count = Math.min(segments.length, count + Math.max(1, Math.ceil((segments.length - count) / 6)));
      shown.current = segments.slice(0, count).join("");
      setVisible(shown.current);
      if (count < segments.length) frame = requestAnimationFrame(paint);
    };
    frame = requestAnimationFrame(paint);
    return () => cancelAnimationFrame(frame);
  }, [streaming, text]);

  return streaming ? visible : text;
}

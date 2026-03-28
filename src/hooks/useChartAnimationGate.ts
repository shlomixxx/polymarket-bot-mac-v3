import { useEffect, useRef, useState } from "react";

/**
 * מפעיל אנימציה בגרף רק כשיש שינוי משמעותי (אורך סדרה או ערך אחרון מעל epsilon).
 * מפחית עומס CPU ברענון תכוף כשהקו כמעט לא משתנה.
 */
export function useChartAnimationGate(
  seriesLength: number,
  lastNumericValue: number | null | undefined,
  options?: { epsilon?: number },
): boolean {
  const eps = options?.epsilon ?? 1e-6;
  const prev = useRef<{ len: number; last: number | null }>({ len: -1, last: null });
  const [animate, setAnimate] = useState(true);

  useEffect(() => {
    const last =
      lastNumericValue != null && Number.isFinite(lastNumericValue) ? lastNumericValue : null;
    const prevLen = prev.current.len;
    const prevLast = prev.current.last;
    const lenChanged = seriesLength !== prevLen;
    const lastChanged =
      last !== null && prevLast !== null && Math.abs(last - prevLast) > eps;
    const significant = prevLen < 0 || lenChanged || seriesLength === 0 || lastChanged;
    prev.current = { len: seriesLength, last };
    setAnimate(significant);
  }, [seriesLength, lastNumericValue, eps]);

  return animate;
}

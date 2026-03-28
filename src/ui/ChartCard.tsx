import type { ReactNode } from "react";

type Props = {
  title: string;
  subtitle?: string;
  height?: number;
  children: ReactNode;
  className?: string;
};

export function ChartCard({ title, subtitle, height, children, className = "" }: Props) {
  return (
    <div className={`chart-card ${className}`.trim()} style={height ? { minHeight: height } : undefined}>
      <h3 className="chart-card__title">{title}</h3>
      {subtitle ? <p className="chart-card__subtitle">{subtitle}</p> : null}
      <div style={{ minWidth: 0 }}>{children}</div>
    </div>
  );
}

import type { CSSProperties, ReactNode } from "react";

type Props = {
  children: ReactNode;
  className?: string;
  style?: CSSProperties;
  padding?: "sm" | "md" | "lg";
};

const pad: Record<NonNullable<Props["padding"]>, string> = {
  sm: "var(--s-3)",
  md: "var(--s-4)",
  lg: "var(--s-5)",
};

export function Card({ children, className = "", style, padding = "md" }: Props) {
  return (
    <div className={`ui-card ${className}`.trim()} style={{ padding: pad[padding], ...style }}>
      {children}
    </div>
  );
}

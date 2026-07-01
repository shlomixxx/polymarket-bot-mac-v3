import type { CSSProperties, ReactNode } from "react";

type Props = {
  title: string;
  subtitle?: string;
  icon?: ReactNode;
  defaultOpen?: boolean;
  children: ReactNode;
  className?: string;
  style?: CSSProperties;
};

/**
 * Dependency-free disclosure built on native <details>/<summary> — keyboard-accessible,
 * remembers open/closed for free, and styled to match the app's card system (tokens only).
 * Use it to tuck advanced settings, long explanations, or raw detail out of the way so the
 * default view stays clean and inviting.
 */
export function Collapsible({
  title,
  subtitle,
  icon,
  defaultOpen = false,
  children,
  className = "",
  style,
}: Props) {
  return (
    <details
      className={`ui-collapsible ${className}`.trim()}
      open={defaultOpen}
      style={{
        background: "var(--card)",
        border: "1px solid var(--border)",
        borderRadius: "var(--radius-md)",
        boxShadow: "var(--shadow-card)",
        ...style,
      }}
    >
      <summary
        style={{
          listStyle: "none",
          cursor: "pointer",
          display: "flex",
          alignItems: "center",
          gap: "var(--s-2)",
          padding: "var(--s-3) var(--s-4)",
          userSelect: "none",
        }}
      >
        <span
          aria-hidden
          className="ui-collapsible__chevron"
          style={{
            color: "var(--muted)",
            fontSize: "0.7rem",
            transition: "transform 0.15s ease",
            display: "inline-block",
          }}
        >
          ▶
        </span>
        {icon != null && <span aria-hidden>{icon}</span>}
        <span style={{ display: "flex", flexDirection: "column", gap: 2, minWidth: 0 }}>
          <span style={{ fontWeight: 600, fontSize: "0.9375rem", color: "var(--text)" }}>{title}</span>
          {subtitle && (
            <span style={{ fontSize: "0.8125rem", color: "var(--muted)" }}>{subtitle}</span>
          )}
        </span>
      </summary>
      <div style={{ padding: "0 var(--s-4) var(--s-4)" }}>{children}</div>
    </details>
  );
}

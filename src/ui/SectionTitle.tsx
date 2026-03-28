import type { ReactNode } from "react";

type Props = { children: ReactNode; as?: "h2" | "h3"; className?: string };

export function SectionTitle({ children, as: Tag = "h2", className = "" }: Props) {
  return <Tag className={`section-title ${className}`.trim()}>{children}</Tag>;
}

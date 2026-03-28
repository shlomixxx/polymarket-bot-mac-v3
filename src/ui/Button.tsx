import type { ButtonHTMLAttributes, ReactNode } from "react";

type Variant = "primary" | "ghost" | "danger";

type Props = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: Variant;
  children: ReactNode;
};

export function Button({ variant = "primary", className = "", children, ...rest }: Props) {
  const cls =
    variant === "primary"
      ? "ui-btn ui-btn--primary"
      : variant === "danger"
        ? "ui-btn ui-btn--danger"
        : "ui-btn ui-btn--ghost";
  return (
    <button type="button" className={`${cls} ${className}`.trim()} {...rest}>
      {children}
    </button>
  );
}

import type { ReactNode } from "react";
import { Link } from "react-router";

interface Props {
  to: string;
  title: string;
  children: ReactNode;
  variant?: "default" | "monospace" | "smallMonospace";
}

const linkClassName = "text-blue-600 dark:text-blue-400 underline hover:text-blue-800 dark:hover:text-blue-300";
const variants = {
  default: linkClassName,
  monospace: `font-mono ${linkClassName}`,
  smallMonospace: `font-mono text-xs ${linkClassName}`,
};

export default function EntityLink({ to, title, children, variant = "default" }: Props) {
  return (
    <Link to={to} className={variants[variant]} title={title}>
      {children}
    </Link>
  );
}

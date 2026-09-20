import { resolve } from "../lib/router";

interface Props {
  href?: string;
  label?: string;
  className?: string;
}

export default function BackButton({ href, label = "← Back", className }: Props) {
  return (
    <a
      href={resolve(href ?? "/")}
      className={
        className ||
        "text-sm text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300 hover:underline"
      }
    >
      {label}
    </a>
  );
}

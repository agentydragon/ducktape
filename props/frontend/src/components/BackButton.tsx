import { Link } from "react-router";

interface Props {
  href?: string;
  className?: string;
}

export default function BackButton({ href, className }: Props) {
  return (
    <Link
      to={href ?? "/"}
      className={
        className ||
        "text-sm text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300 hover:underline"
      }
    >
      ← Back
    </Link>
  );
}

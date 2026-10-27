import { Fragment } from "react";
import { ChevronRight } from "../lib/icons";
import { Link } from "react-router";

interface BreadcrumbItem {
  label: string;
  href?: string;
}

interface Props {
  items: BreadcrumbItem[];
}

export default function Breadcrumb({ items }: Props) {
  return (
    <nav className="flex items-center gap-1 text-sm text-gray-600 dark:text-gray-400">
      {items.map((item, index) => (
        <Fragment key={index}>
          {index > 0 && <ChevronRight size={16} className="text-gray-400 dark:text-gray-500" />}
          {item.href ? (
            <Link to={item.href} className="hover:text-gray-900 dark:hover:text-gray-100 hover:underline">
              {item.label}
            </Link>
          ) : (
            <span className="text-gray-900 dark:text-gray-100 font-medium">{item.label}</span>
          )}
        </Fragment>
      ))}
    </nav>
  );
}

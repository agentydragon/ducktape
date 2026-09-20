import type { ReactNode } from "react";
import { Button } from "@mantine/core";

interface Props {
  active: boolean;
  onClick: () => void;
  children: ReactNode;
}

export default function TabButton({ active, onClick, children }: Props) {
  return (
    <Button
      unstyled
      type="button"
      className={`px-4 py-2 font-medium text-sm border-b-2 ${
        active
          ? "border-blue-500 text-blue-600 dark:text-blue-400"
          : "border-transparent text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300"
      }`}
      onClick={onClick}
    >
      {children}
    </Button>
  );
}

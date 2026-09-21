import { useEffect, useRef, useState } from "react";
import { Button } from "@mantine/core";
import { Check, Copy } from "../lib/icons";
import { toast } from "../lib/toast";

interface Props {
  text: string;
  label?: string;
  successMessage?: string;
}

export default function CopyButton({ text, label = "Copy", successMessage = "Copied to clipboard" }: Props) {
  const [copied, setCopied] = useState(false);
  const copyTimeout = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(
    () => () => {
      if (copyTimeout.current) clearTimeout(copyTimeout.current);
    },
    []
  );

  async function copy() {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      toast.success(successMessage);
      if (copyTimeout.current) clearTimeout(copyTimeout.current);
      copyTimeout.current = setTimeout(() => setCopied(false), 2000);
    } catch (err) {
      toast.error("Failed to copy to clipboard");
      console.error("Copy failed:", err);
    }
  }

  return (
    <Button
      unstyled
      onClick={() => void copy()}
      type="button"
      className="inline-flex items-center gap-1 px-2 py-1 text-xs font-medium text-gray-700 dark:text-gray-300 bg-white dark:bg-gray-700 border border-gray-300 dark:border-gray-600 rounded hover:bg-gray-50 dark:hover:bg-gray-600 focus:outline-none focus:ring-2 focus:ring-blue-500"
      title={label}
    >
      {copied ? (
        <>
          <Check size={14} className="text-green-600 dark:text-green-400" />
          <span className="text-green-600 dark:text-green-400">Copied!</span>
        </>
      ) : (
        <>
          <Copy size={14} />
          <span>{label}</span>
        </>
      )}
    </Button>
  );
}

import { notifications } from "@mantine/notifications";
import type { ReactNode } from "react";

// The single mechanism for surfacing action outcomes to the operator. Failures
// (launch, feedback, a click that didn't commit) route here as a red toast rather
// than inline/quiet, so a 502/timeout is always visible instead of being swallowed.
export function toastError(title: string, error: unknown): void {
  notifications.show({
    color: "red",
    title,
    message: error instanceof Error ? error.message : String(error),
    autoClose: 8000,
  });
}

export function toastSuccess(title: string, message?: ReactNode): void {
  notifications.show({ color: "teal", title, message, autoClose: 6000 });
}

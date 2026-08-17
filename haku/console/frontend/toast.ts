import { notifications } from "@mantine/notifications";
import type { ReactNode } from "react";

import { SUCCESS_COLOR } from "./theme";

// The single mechanism for surfacing action outcomes to the operator. Failures (launch, feedback, a
// click that didn't commit) route here as a red toast, so a 502/timeout is visible rather than
// swallowed.
export function toastError(title: string, error: unknown): void {
  notifications.show({
    color: "red",
    title,
    message: error instanceof Error ? error.message : String(error),
    autoClose: 8000,
  });
}

export function toastSuccess(title: string, message?: ReactNode): void {
  notifications.show({ color: SUCCESS_COLOR, title, message, autoClose: 6000 });
}

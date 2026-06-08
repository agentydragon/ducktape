import { notifications } from "@mantine/notifications";

// Surface a failed backend fetch as a red toast instead of letting it fail quietly — a 502/timeout
// on a request should be visible, not just leave the UI on stale data. A stable `id` per source
// coalesces repeated failures into one toast rather than stacking a new one per retry.
export function toastFetchError(id, title, error) {
  notifications.show({ id, color: "red", title, message: error?.message || String(error), autoClose: 8000 });
}

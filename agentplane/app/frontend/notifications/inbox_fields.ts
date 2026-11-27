/** The `Inbox` fields (proto paths) a reader watches. A watch sends a frame only when one of them changes. */

/** The sidebar's badge. Omits `delivery.next_work_at`, which flips with every worker claim and is shown nowhere here. */
export const THREAD_NOTICE_FIELDS = [
  "delivery_error",
  "latest_notice.error",
  "latest_notice.confirmed",
  "delivery.unannounced_count",
  "delivery.pending_acknowledgement_count",
  "delivery.notice_due_at",
] as const;

/** The notification status page, which shows the inbox's whole delivery state including the next scheduled work. */
export const NOTIFICATION_STATUS_FIELDS = [
  "last_cursor",
  "acknowledged",
  "covered",
  "expired_through",
  "retired",
  "delivery_error",
  "latest_notice",
  "delivery",
] as const;

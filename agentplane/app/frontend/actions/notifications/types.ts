export interface ActionNotificationContent {
  title: string;
  text: string;
}

/** The notification-specific contribution; the service worker combines it with the caller title. */
export interface ActionNotificationParts {
  actionTitle: string;
  text: string;
}

export interface ActionNotificationContent {
  title: string;
  text: string;
}

/** The action's contribution; the service-worker formatter combines actionTitle with request.title. */
export interface ActionNotificationParts {
  actionTitle: string;
  text: string;
}

# Notifications

Shared notification status presentation and operator projection types for Thread and Sandbox views. Thread-specific notice copy and grouping remain in `../threads/`; this package has no dependency on either consumer.

`NotificationStatus` is the read-only status drawer, and `types.ts` describes the notification service snapshots consumed by the frontend. Bazel targets are declared in `BUILD.bazel`.

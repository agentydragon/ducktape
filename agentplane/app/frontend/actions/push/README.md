# Push

This package owns Action approval Web Push settings and the service worker that presents and handles those notifications. The worker bundle is served at the existing `/sw.js` URL by `frontend/serving`.

The notification rendering in `actions/notifications` remains the shared Action presentation used by the worker. This package is separate from the NotificationService inbox UI in `frontend/notifications`.

# Push

This package owns Action approval Web Push settings and the service worker that presents and handles those notifications. The worker bundle is served at the existing `/sw.js` URL by `frontend/serving`.

The notification rendering in `actions/notifications` remains the shared Action presentation used by the worker. This package is separate from the NotificationService inbox UI in `frontend/notifications`.

When one waiting Action has a supported integration preview, this browser can opt in to showing
live Gmail subjects or Tana node names in its notification. The preference defaults to off and
is stored in the browser for both the page and worker. The authenticated worker reads the names
from the saved request at delivery time. Delivery still works when the lookup fails. The web push
payload carries no integration metadata.

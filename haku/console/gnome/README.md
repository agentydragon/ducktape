# Haku Approvals GNOME integration

The extension adds a Haku Approvals indicator to the GNOME panel. Its one-click action launches
the GTK/WebKit companion window, which loads the chrome-free `/_console/approvals-embed` route from
the console. The route uses the console's existing pending-call cards, per-server renderers, live
event socket, and exact-origin decision endpoints.

The companion starts in the background with the GNOME session. It keeps its own persistent WebKit
cookie store and live console connection, so a new pending tool call raises the small window. The
first click on the panel indicator shows the window for the operator to complete Authentik login;
the WebKit session is separate from the default browser's cookies.

`HAKU_CONSOLE_URL` can override the default `https://haku.allegedly.works` origin for local or
staging console deployments.

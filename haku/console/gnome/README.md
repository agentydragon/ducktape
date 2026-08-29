# Haku Approvals desktop application

Haku Approvals is a standalone GTK/WebKit application. It loads the chrome-free
`/_console/approvals-embed` route from the console, reusing the console's existing pending-call
cards, per-server renderers, live event socket, and exact-origin decision endpoints.

The Home Manager module starts it in the background with the graphical session. It keeps its own
persistent WebKit cookie store and live console connection, so a new pending tool call raises the
small window. Launching Haku Approvals from the application menu shows the window explicitly; the
first background launch also leaves the window visible until Authentik login completes. The
WebKit session is separate from the default browser's cookies.

The package disables WebKitGTK's dmabuf renderer by default because the Mutter/Wayland stack on
Wyrm2 rejects its initial explicit-sync commit. Set `WEBKIT_DISABLE_DMABUF_RENDERER=0` to test the
hardware path explicitly.

`HAKU_CONSOLE_URL` can override the default `https://haku.allegedly.works` origin for local or
staging console deployments.

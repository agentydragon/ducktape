self.addEventListener("push", (event) => {
  if (!event.data) return;
  const message = event.data.json();
  if (message.kind === "retract") {
    event.waitUntil(
      self.registration.showNotification(message.outcome, {
        tag: message.action_id,
        silent: true,
        requireInteraction: false,
        data: message,
      })
    );
    return;
  }
  if (message.kind !== "show") return;
  event.waitUntil(
    (async () => {
      // Push services can deliver an old show after its retraction. Re-read authority before
      // offering decisions; an expired session gets a reauthentication deep link, not buttons.
      let current = null;
      try {
        const response = await fetch(`/actions/${encodeURIComponent(message.action_id)}`, { credentials: "include" });
        if (response.ok) current = await response.json();
      } catch {
        /* Offline delivery still opens the app without offering stale decisions. */
      }
      if (current && current.state !== "decision_pending") {
        await self.registration.showNotification(current.state.replaceAll("_", " "), {
          tag: message.action_id,
          silent: true,
          data: { kind: "retract", action_id: message.action_id },
        });
        return;
      }
      await self.registration.showNotification(`${message.action_group} / ${message.action_name}`, {
        body: current ? "Action requires approval" : "Open Agentplane to review this Action",
        tag: message.action_id,
        requireInteraction: true,
        actions: current
          ? [
              { action: "approve", title: "Approve" },
              { action: "deny", title: "Deny" },
            ]
          : [],
        data: current ? { ...message, version: current.version } : message,
      });
    })()
  );
});
self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const message = event.notification.data;
  if (!message || message.kind !== "show") return;
  if (event.action !== "approve" && event.action !== "deny") {
    event.waitUntil(self.clients.openWindow("/#/actions"));
    return;
  }
  event.waitUntil(
    fetch(`/push/decision/${encodeURIComponent(message.action_id)}`, {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        verdict: event.action === "approve" ? "allow" : "deny",
        expected_version: message.version,
        idempotency_key: crypto.randomUUID(),
        decision_note: null,
      }),
    }).then((response) => {
      if (!response.ok) return self.clients.openWindow("/#/actions");
    })
  );
});

self.addEventListener("install", (event) => event.waitUntil(self.skipWaiting()));
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

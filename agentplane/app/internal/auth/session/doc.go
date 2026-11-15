// Package session owns SQL persistence for the app's browser sessions. Its inputs are a verified
// handle's one-way ID, pending OIDC login state, and the projected operator login and token values;
// it returns session records and opaque handles. Refresh and termination serialize on the same
// database row: refresh runs its replacement callback under a row lock and commits token rotation
// atomically, while termination deletes and notifies in one transaction. The package owns expiry,
// pending-state merging, and database notifications. The parent auth package retains HTTP/OIDC
// behavior, cookie signing, login policy, and the projection shown to callers.
package session

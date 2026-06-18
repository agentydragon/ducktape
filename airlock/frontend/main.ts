import "./app.css";
import { mount } from "svelte";
import App from "./App.svelte";
import { isAuthCallback, handleAuthCallback } from "./auth.ts";

// Handle the OIDC callback before mounting the app. A failed or re-loaded
// callback (single-use `code` already spent, PKCE state gone from sessionStorage)
// must NOT block the mount — otherwise `#app` stays empty and the page is a blank
// dead-end that reloading only re-breaks. On error, strip the stale code/state
// from the URL and mount anyway; App re-initiates a clean login.
if (isAuthCallback()) {
  try {
    await handleAuthCallback();
  } catch (e) {
    console.error("OIDC callback failed; restarting login", e);
    window.history.replaceState({}, "", "/");
  }
}

const root = document.getElementById("app");
if (!root) throw new Error("No #app element");

mount(App, { target: root });

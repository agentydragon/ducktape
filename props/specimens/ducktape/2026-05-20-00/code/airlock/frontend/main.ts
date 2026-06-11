import "./app.css";
import { mount } from "svelte";
import App from "./App.svelte";
import { isAuthCallback, handleAuthCallback } from "./auth.ts";

// Handle OIDC callback before mounting the app.
if (isAuthCallback()) {
  await handleAuthCallback();
}

const root = document.getElementById("app");
if (!root) throw new Error("No #app element");

mount(App, { target: root });

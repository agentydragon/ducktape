// Smoke-bundle entry. Mounts the smoke shell into `#app`. Lives under the
// debundle subpackage so the smoke bundle build can reach it without crossing
// the production library's package boundary; the production `:app` library
// stays package-private and untouched.

import SmokeShell from "./smoke_shell.svelte";
import { mount } from "svelte";

const app = mount(SmokeShell, {
  target: document.getElementById("app")!,
});

export default app;

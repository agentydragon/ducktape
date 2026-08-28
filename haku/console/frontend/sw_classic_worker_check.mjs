// Guards the emitted service-worker bundle against the one regression that ships a broken worker
// while every other check stays green: `push_subscription.ts` registers `/sw.js` as a *classic*
// worker (a bare `navigator.serviceWorker.register("/sw.js")`, no `{ type: "module" }`), so the
// bundle must parse under the script goal. A single top-level `export`/`import` is
// `Uncaught SyntaxError: Unexpected token 'export'` in a classic worker, and the worker never
// installs — no Web Push, no offline. It shipped once because the esbuild `service_worker` target
// emitted `format = "esm"` while `sw.ts` exported `notificationActions` for its unit test; nothing
// inspected the *bundle*, since `sw.test.ts` imports the TypeScript source, where the export is
// correct and expected.
//
// `node:vm` compiles source text under the same script goal a classic worker uses, so a top-level
// `export`/`import` throws here exactly as it does in the browser. That is a behavioral check, not
// a grep for the substring "export" — which would misfire on a string literal and miss re-export
// forms.
import { readFileSync } from "node:fs";
import vm from "node:vm";

/** Whether `code` parses as a classic script (the goal a `type: "classic"` worker is loaded with).
 * Compilation checks syntax only, so the bundle's references to browser globals never run. */
function parsesAsClassicScript(code) {
  try {
    new vm.Script(code, { filename: "sw.js" });
    return true;
  } catch (error) {
    if (error instanceof SyntaxError) return false;
    throw error;
  }
}

const bundlePath = process.env.SW_BUNDLE;
if (!bundlePath) throw new Error("SW_BUNDLE is not set");
const bundle = readFileSync(bundlePath, "utf8");
if (bundle.length === 0) throw new Error(`emitted service worker ${bundlePath} is empty`);

// Anti-vacuity: the goal really rejects module syntax, so a passing bundle means something.
if (parsesAsClassicScript("export const x = 1;")) {
  throw new Error("classic-script check is vacuous: it accepted a module-only top-level export");
}

if (!parsesAsClassicScript(bundle)) {
  throw new Error(
    `${bundlePath} is not a valid classic service worker: it has top-level ESM syntax ` +
      "(export/import), which is `Uncaught SyntaxError: Unexpected token 'export'` in a classic " +
      'worker. The esbuild `service_worker` target must emit `format = "iife"`.'
  );
}

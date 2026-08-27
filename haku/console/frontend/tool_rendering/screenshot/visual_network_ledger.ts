/**
 * The in-page half of the screenshot harnesses' network contract, read by the drivers through
 * `assertNetworkSettled` (util/testing/frontend_visual/capture.mjs): every stubbed fetch is
 * tracked while in flight, and anything that must fail the run — an unmatched route, a rejected
 * fetch, an unhandled promise rejection — is recorded as a violation. The drivers refuse to
 * capture while requests are pending and fail the scene on any violation, so a missing mock or a
 * data race is a named red test instead of a plausible-looking screenshot.
 */

export interface VisualNetworkLedger {
  pending: string[];
  violations: string[];
}

declare global {
  interface Window {
    __visualNetworkLedger__?: VisualNetworkLedger;
  }
}

/** Create the ledger (and its unhandled-rejection recorder). Call when installing a fetch stub. */
export function ensureLedger(): VisualNetworkLedger {
  if (!window.__visualNetworkLedger__) {
    window.__visualNetworkLedger__ = { pending: [], violations: [] };
    window.addEventListener("unhandledrejection", (event) => {
      recordViolation(`unhandled rejection: ${String(event.reason)}`);
    });
  }
  return window.__visualNetworkLedger__;
}

export function recordViolation(text: string): void {
  ensureLedger().violations.push(text);
}

/** Run one stubbed fetch under the ledger: pending while it settles, a rejection recorded. */
export async function tracked(url: string, respond: () => Promise<Response>): Promise<Response> {
  const { pending } = ensureLedger();
  pending.push(url);
  try {
    return await respond();
  } catch (error) {
    recordViolation(`fetch rejected: ${url}: ${String(error)}`);
    throw error;
  } finally {
    pending.splice(pending.indexOf(url), 1);
  }
}

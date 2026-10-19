import type { GeolocationOptions } from "@haku/console-bridge/protocol";

export interface GeolocationApproval {
  id: string;
  mode: "geolocation" | "geolocationWatch";
  options?: GeolocationOptions;
  createdAt: string;
}

export interface ScreenshotApproval {
  id: string;
  createdAt: string;
}

export type CapabilityRequestItem =
  | { kind: "geolocation"; id: string; createdAt: string; approval: GeolocationApproval }
  | { kind: "screenshot"; id: string; createdAt: string; approval: ScreenshotApproval };

function createdAtMs(value: string): number {
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? 0 : parsed;
}

export function geolocationApprovalQueueId(id: string): string {
  return `geolocation:${id}`;
}

export function screenshotApprovalQueueId(id: string): string {
  return `screenshot:${id}`;
}

export function capabilityRequestItems(
  geolocationRequests: readonly GeolocationApproval[],
  screenshotRequests: readonly ScreenshotApproval[]
): CapabilityRequestItem[] {
  return [
    ...geolocationRequests.map((approval): CapabilityRequestItem => ({
      kind: "geolocation",
      id: geolocationApprovalQueueId(approval.id),
      createdAt: approval.createdAt,
      approval,
    })),
    ...screenshotRequests.map((approval): CapabilityRequestItem => ({
      kind: "screenshot",
      id: screenshotApprovalQueueId(approval.id),
      createdAt: approval.createdAt,
      approval,
    })),
  ].sort((a, b) => createdAtMs(b.createdAt) - createdAtMs(a.createdAt));
}

export function geolocationApprovalTitle(approval: GeolocationApproval): string {
  return approval.mode === "geolocationWatch" ? "Allow continuous location sharing?" : "Allow location sharing?";
}

export function geolocationApprovalBody(approval: GeolocationApproval): string {
  return approval.mode === "geolocationWatch"
    ? "Haku is asking to continuously receive your device location until it stops the watch or you withdraw access."
    : "Haku is asking to read your current device location once.";
}

export const SCREENSHOT_APPROVAL_TITLE = "Allow screen capture for a screenshot?";
export const SCREENSHOT_APPROVAL_BODY: string =
  "Haku is asking to capture a screenshot of this page. Approving opens your browser's own " +
  "share-this-tab picker; once granted, further screenshots are instant until you withdraw or " +
  "stop sharing.";

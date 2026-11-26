import NoVncRfb from "@novnc/novnc";

interface RfbEvents {
  connect: Event;
  disconnect: CustomEvent<{ clean: boolean }>;
}

export interface RfbConnection {
  scaleViewport: boolean;
  resizeSession: boolean;
  addEventListener<K extends keyof RfbEvents>(event: K, listener: (event: RfbEvents[K]) => void): void;
  removeEventListener<K extends keyof RfbEvents>(event: K, listener: (event: RfbEvents[K]) => void): void;
  disconnect(): void;
}

type RfbConstructor = new (target: HTMLElement, url: string) => RfbConnection;

// noVNC's JavaScript constructor implements the browser API used by our viewers.
export const RFB: RfbConstructor = NoVncRfb as RfbConstructor;

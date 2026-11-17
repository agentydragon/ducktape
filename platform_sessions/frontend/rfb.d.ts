declare module "@novnc/novnc" {
  export default class RFB {
    constructor(target: HTMLElement, url: string);
    scaleViewport: boolean;
    resizeSession: boolean;
    addEventListener(event: "connect" | "disconnect", listener: () => void): void;
    removeEventListener(event: "connect" | "disconnect", listener: () => void): void;
    disconnect(): void;
  }
}

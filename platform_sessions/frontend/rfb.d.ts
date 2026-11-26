declare module "@novnc/novnc" {
  export default class RFB {
    constructor(target: HTMLElement, url: string);
    scaleViewport: boolean;
    resizeSession: boolean;
    addEventListener(event: "connect", listener: () => void): void;
    addEventListener(event: "disconnect", listener: (event: CustomEvent<{ clean: boolean }>) => void): void;
    removeEventListener(event: "connect", listener: () => void): void;
    removeEventListener(event: "disconnect", listener: (event: CustomEvent<{ clean: boolean }>) => void): void;
    disconnect(): void;
  }
}

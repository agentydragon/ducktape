import { generatedPromise, onEvent } from "./contract";

export function listen(): void {
  onEvent(async () => {
    await generatedPromise();
  });
}

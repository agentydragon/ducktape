/** Change listeners for a `useSyncExternalStore` source; subclasses call `notify()` after a change. */
export class Listeners {
  readonly #listeners = new Set<() => void>();

  subscribe = (listener: () => void): (() => void) => {
    this.#listeners.add(listener);
    return () => this.#listeners.delete(listener);
  };

  notify(): void {
    for (const listener of this.#listeners) listener();
  }
}

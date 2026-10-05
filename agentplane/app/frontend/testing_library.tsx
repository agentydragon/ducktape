import { MantineProvider } from "@mantine/core";
import "@testing-library/jest-dom/vitest";
import { cleanup, render } from "@testing-library/react";
import userEvent, { type UserEvent } from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, vi } from "vitest";

// vitest's `globals` is off, so Testing Library registers neither its `afterEach(cleanup)` nor its
// act environment on import.
(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
afterEach(cleanup);

// While it waits (`waitFor`, `findBy*`, user-event), Testing Library advances only Jest's fake
// timers, found through a `jest` global. Under vitest's fake timers that wait never ends, so give
// it one that advances vitest's.
(globalThis as typeof globalThis & { jest: { advanceTimersByTime: (ms: number) => void } }).jest = {
  advanceTimersByTime: (ms) => vi.advanceTimersByTime(ms),
};

/** `ui` rendered in the app's Mantine provider, and the `user` to interact with it. The `user` waits
 * on no timer between actions (`delay: null`), so it works under `vi.useFakeTimers()` too. */
export function renderInMantine(ui: ReactNode): ReturnType<typeof render> & { user: UserEvent } {
  return Object.assign(
    render(ui, { wrapper: ({ children }) => <MantineProvider env="test">{children}</MantineProvider> }),
    { user: userEvent.setup({ delay: null }) }
  );
}

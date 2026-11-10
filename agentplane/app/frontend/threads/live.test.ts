import { expect, it } from "vitest";

import { combinedConnection } from "./live";

it.each([
  {
    history: "the harness dropped this stream",
    metadata: "the harness dropped this stream",
    expected: "the harness dropped this stream",
  },
  {
    history: "HTTP 503",
    metadata: "stream stopped sending frames",
    expected: "HTTP 503; stream stopped sending frames",
  },
])("names each distinct reason the watches dropped for once: $expected", ({ history, metadata, expected }) => {
  const combined = combinedConnection(
    { phase: "reconnecting", since: 2_000, attempt: 1, lastError: history },
    { phase: "reconnecting", since: 1_000, attempt: 3, lastError: metadata }
  );
  expect(combined).toEqual({ phase: "reconnecting", since: 1_000, attempt: 3, lastError: expected });
});

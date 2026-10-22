// @vitest-environment happy-dom
import { expect, it } from "vitest";
import { liveNdjsonWatch, publishNdjsonWatch } from "./network";

it("publishes deltas only to matching live watches and removes canceled readers", async () => {
  const updates: string[] = [];
  const response = liveNdjsonWatch(
    "/upstream/actions/v1/requests:watch",
    [{ snapshotComplete: {} }],
    () => {
      updates.push("published");
      return [{ upsert: { id: "changed" } }];
    },
    undefined
  );
  const reader = response.body!.pipeThrough(new TextDecoderStream()).getReader();
  expect((await reader.read()).value).toBe('{"result":{"snapshotComplete":{}}}\n');
  publishNdjsonWatch("other:watch");
  expect(updates).toEqual([]);
  publishNdjsonWatch("requests:watch");
  expect((await reader.read()).value).toBe('{"result":{"upsert":{"id":"changed"}}}\n');
  expect(updates).toEqual(["published"]);
  await reader.cancel();
  publishNdjsonWatch("requests:watch");
  expect(updates).toEqual(["published"]);
});

it("removes an aborted watch and honors a signal already aborted at construction", async () => {
  for (const alreadyAborted of [false, true]) {
    const stopped = new AbortController();
    let published = false;
    if (alreadyAborted) stopped.abort();
    const response = liveNdjsonWatch(
      "/abort:watch",
      [],
      () => {
        published = true;
        return [];
      },
      stopped.signal
    );
    const reader = response.body!.getReader();
    if (!alreadyAborted) stopped.abort();
    await expect(reader.read()).rejects.toThrow("aborted");
    publishNdjsonWatch("abort:watch");
    expect(published).toBe(false);
  }
});

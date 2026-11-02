import { create } from "@bufbuild/protobuf";
import { afterEach, expect, it, vi } from "vitest";

import { HttpError } from "../client";
import { SessionSpecSchema, Harness } from "../../../proto/session_pb";
import { DeliveryState } from "../../../runner_gateway/dispatcher/session_dispatcher_pb";
import { openSession, getSessionOpening, sessionOpeningRejected, sessionOpeningNotVisible } from "./client";

const id = "b351b316-fdc4-4f50-a642-758992ae3323";
afterEach(() => vi.unstubAllGlobals());

it.each([undefined, null, "", "setup"])(
  "keeps the existing HTTP request and projected response for setup %s",
  async (setup) => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(
        Response.json(
          { sessionId: id, sandbox: "test", sandboxUid: "uid", state: "DELIVERY_STATE_QUEUED_IN_DISPATCHER" },
          { status: 201 }
        )
      );
    vi.stubGlobal("fetch", fetchMock);
    const opening = await openSession(
      "test",
      "uid",
      id,
      create(SessionSpecSchema, { harness: Harness.CODEX, model: "test-model" }),
      setup
    );
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe("/sandboxes/test/sessions");
    expect(JSON.parse(init.body)).toEqual({
      session_id: id,
      sandbox_uid: "uid",
      spec: { harness: "HARNESS_CODEX", model: "test-model" },
      ...(setup === undefined ? {} : { setup_script: setup }),
    });
    expect(opening.sessionId).toBe(id);
    expect(opening.state).toBe(DeliveryState.QUEUED_IN_DISPATCHER);
  }
);

it("looks up the same Session identity without repeating an uncertain Open", async () => {
  const fetchMock = vi
    .fn()
    .mockResolvedValueOnce(Response.json({ code: 4, message: "uncertain", detail: "uncertain" }, { status: 504 }))
    .mockResolvedValueOnce(Response.json({ sessionId: id, sandbox: "test", state: "DELIVERY_STATE_RUNNER_ADMITTED" }));
  vi.stubGlobal("fetch", fetchMock);
  await expect(openSession("test", "uid", id, create(SessionSpecSchema))).rejects.toMatchObject({ code: 4 });
  const opening = await getSessionOpening("test", id);
  expect(opening.state).toBe(DeliveryState.RUNNER_ADMITTED);
  expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
    "/sandboxes/test/sessions",
    `/sandboxes/test/sessions/${id}/opening`,
  ]);
});

it.each([1, 2, 4, 8, 13, 14, 15])("keeps gRPC outcome %s uncertain", (code) => {
  expect(sessionOpeningRejected(new HttpError(new Response(null, { status: 400 }), { code }))).toBe(false);
});

it.each([
  [5, 404],
  [14, 503],
])("recognizes unobserved gRPC %s and legacy HTTP %s openings", (code, httpStatus) => {
  expect(sessionOpeningNotVisible(new HttpError(new Response(null, { status: httpStatus }), { code }))).toBe(true);
  expect(
    sessionOpeningNotVisible(new HttpError(new Response(null, { status: httpStatus }), { detail: "unobserved" }))
  ).toBe(true);
});

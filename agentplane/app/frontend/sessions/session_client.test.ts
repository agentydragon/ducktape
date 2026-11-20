import { create } from "@bufbuild/protobuf";
import { afterEach, expect, it, vi } from "vitest";

import { HttpError } from "../transport/client";
import { EnvironmentTargetSchema } from "../../../proto/identity_pb";
import { Harness, SessionSpecSchema } from "../../../proto/session_pb";
import { SessionStatus_Materialization } from "../../../session_dispatcher/dispatcher/api/session/session_dispatcher_pb";
import {
  createSession,
  getSession,
  listIdentitySessions,
  sessionCreateRejected,
  sessionNotVisible,
} from "./session_client";

const id = "b351b316-fdc4-4f50-a642-758992ae3323";
const target = create(EnvironmentTargetSchema, { namespace: "team", name: "test", uid: "uid" });
afterEach(() => vi.unstubAllGlobals());

it.each([undefined, null, "", "setup"])(
  "sends the typed CreateSession request and projected Session for setup %s",
  async (setup) => {
    const fetchMock = vi.fn().mockResolvedValue(
      Response.json(
        {
          sessionId: id,
          status: { materialization: "SESSION_MATERIALIZATION_WAITING" },
          placement: {
            environmentHosted: {
              environment: { name: "test", uid: "uid" },
              harness: "HARNESS_CODEX",
            },
          },
        },
        { status: 201 }
      )
    );
    vi.stubGlobal("fetch", fetchMock);
    const session = await createSession(
      target,
      id,
      create(SessionSpecSchema, {
        placement: {
          kind: {
            case: "environmentHosted",
            value: { environment: { name: target.name, uid: target.uid }, harness: Harness.CODEX },
          },
        },
        model: "test-model",
      }),
      setup
    );
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe("/sandboxes/test/sessions");
    expect(JSON.parse(init.body)).toEqual({
      session_id: id,
      sandbox_uid: "uid",
      spec: {
        placement: {
          environmentHosted: {
            environment: { name: "test", uid: "uid" },
            harness: "HARNESS_CODEX",
          },
        },
        model: "test-model",
        cwd: "",
        reasoningEffort: "",
        instructions: "",
      },
      ...(setup === undefined ? {} : { setup_script: setup }),
    });
    expect(session.sessionId).toBe(id);
    expect(session.status?.materialization).toBe(SessionStatus_Materialization.SESSION_MATERIALIZATION_WAITING);
  }
);

it("looks up the same Session identity without repeating an uncertain Create", async () => {
  const fetchMock = vi
    .fn()
    .mockResolvedValueOnce(Response.json({ code: 4, message: "uncertain", detail: "uncertain" }, { status: 504 }))
    .mockResolvedValueOnce(
      Response.json({
        session: {
          sessionId: id,
          status: { materialization: "SESSION_MATERIALIZATION_ACCEPTED" },
          placement: {
            environmentHosted: { environment: { name: "test", uid: "uid" }, harness: "HARNESS_CODEX" },
          },
        },
      })
    );
  vi.stubGlobal("fetch", fetchMock);
  await expect(createSession(target, id, create(SessionSpecSchema))).rejects.toMatchObject({ code: 4 });
  const session = await getSession(id);
  expect(session.status?.materialization).toBe(SessionStatus_Materialization.SESSION_MATERIALIZATION_ACCEPTED);
  expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
    "/sandboxes/test/sessions",
    `/upstream/dispatcher/v1/sessions/${id}`,
  ]);
});

it("keeps a non-JSON intermediary failure uncertain and retains its diagnostics", async () => {
  const fetchMock = vi.fn().mockResolvedValue(new Response("temporary edge failure", { status: 429 }));
  vi.stubGlobal("fetch", fetchMock);
  const error: unknown = await createSession(target, id, create(SessionSpecSchema)).catch((reason: unknown) => reason);

  expect(error).toBeInstanceOf(HttpError);
  expect(error).toMatchObject({ status: 429, body: "temporary edge failure" });
  expect(sessionCreateRejected(error)).toBe(false);
  expect(fetchMock).toHaveBeenCalledTimes(1);
});

it.each([
  { sessionId: "other-session", uid: target.uid, message: /requested Session/ },
  { sessionId: id, uid: "other-environment", message: /different Environment identity/ },
])("keeps a mismatched creation acknowledgement uncertain", async ({ sessionId, uid, message }) => {
  const fetchMock = vi.fn().mockResolvedValue(
    Response.json({
      sessionId,
      placement: { environmentHosted: { environment: { name: target.name, uid } } },
    })
  );
  vi.stubGlobal("fetch", fetchMock);
  const error: unknown = await createSession(target, id, create(SessionSpecSchema)).catch((reason: unknown) => reason);

  expect(error).toBeInstanceOf(Error);
  expect(error).toMatchObject({ message: expect.stringMatching(message) });
  expect(sessionCreateRejected(error)).toBe(false);
  expect(fetchMock).toHaveBeenCalledTimes(1);
});

it("keeps malformed creation acknowledgements uncertain", async () => {
  const fetchMock = vi.fn().mockResolvedValue(Response.json({ sessionId: id, placement: [] }));
  vi.stubGlobal("fetch", fetchMock);
  const error: unknown = await createSession(target, id, create(SessionSpecSchema)).catch((reason: unknown) => reason);

  expect(error).toBeInstanceOf(Error);
  expect(sessionCreateRejected(error)).toBe(false);
  expect(fetchMock).toHaveBeenCalledTimes(1);
});

it("rejects a different Session ID in the Dispatcher response", async () => {
  const fetchMock = vi.fn().mockResolvedValue(
    Response.json({
      session: {
        sessionId: "different-session",
        placement: {
          environmentHosted: { environment: { name: "test", uid: "uid" }, harness: "HARNESS_CODEX" },
        },
      },
    })
  );
  vi.stubGlobal("fetch", fetchMock);

  await expect(getSession(id)).rejects.toThrow(
    `Dispatcher returned Session different-session for requested Session ${id}`
  );
  expect(fetchMock).toHaveBeenCalledTimes(1);
  expect(fetchMock.mock.calls[0]![0]).toBe(`/upstream/dispatcher/v1/sessions/${id}`);
});

it.each([3, 5, 6, 7, 9, 10, 16])("classifies gRPC outcome %s as a rejection", (code) => {
  expect(sessionCreateRejected(new HttpError(new Response(null, { status: 422 }), { code }))).toBe(true);
});

it.each([{ title: "Unprocessable Entity", status: 422 }, { code: "3" }])(
  "keeps an untyped error body uncertain: %o",
  (body) => {
    expect(sessionCreateRejected(new HttpError(new Response(null, { status: 422 }), body))).toBe(false);
  }
);

it.each([1, 2, 4, 8, 13, 14, 15])("keeps gRPC outcome %s uncertain", (code) => {
  expect(sessionCreateRejected(new HttpError(new Response(null, { status: 400 }), { code }))).toBe(false);
});

it.each([
  [5, 404],
  [14, 503],
])("recognizes an unobserved Session from gRPC %s or HTTP %s", (code, httpStatus) => {
  expect(sessionNotVisible(new HttpError(new Response(null, { status: httpStatus }), { code }))).toBe(true);
  expect(sessionNotVisible(new HttpError(new Response(null, { status: httpStatus }), { detail: "unobserved" }))).toBe(
    true
  );
});

it("reads every Session page before deciding an identity has no Session references", async () => {
  const fetch = vi
    .fn()
    .mockResolvedValueOnce(
      Response.json({
        sessions: [{ sessionId: "first", identity: { name: "fake agent", uid: "uid-1" } }],
        nextPageToken: "next",
      })
    )
    .mockResolvedValueOnce(
      Response.json({ sessions: [{ sessionId: "second", identity: { name: "other", uid: "uid-2" } }] })
    );
  vi.stubGlobal("fetch", fetch);
  expect((await listIdentitySessions()).map((session) => session.sessionId)).toEqual(["first", "second"]);
  expect(fetch.mock.calls[1]?.[0]).toContain("pageToken=next");
});

it("refuses a repeated Session page token instead of assuming the inventory is complete", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation(async () => Response.json({ sessions: [], nextPageToken: "repeat" }))
  );
  await expect(listIdentitySessions()).rejects.toThrow("repeated a Session page token");
});

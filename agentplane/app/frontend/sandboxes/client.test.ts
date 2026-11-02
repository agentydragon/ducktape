import { create } from "@bufbuild/protobuf";
import { afterEach, expect, it, vi } from "vitest";

import { HttpError } from "../client";
import {
  EnvironmentRuntimeLifecycleBlockerKind,
  EnvironmentRuntimeLifecycleMode,
  Harness,
  SessionSpecSchema,
} from "../../../proto/session_pb";
import { DeliveryState } from "../../../runner_gateway/dispatcher/session_dispatcher_pb";
import { EnvironmentTargetSchema } from "../../../proto/identity_pb";

import {
  getEnvironmentRuntimeStatus,
  openSession,
  getSessionOpening,
  sessionOpeningRejected,
  sessionOpeningNotVisible,
  resumeSandbox,
  suspendEnvironment,
  deleteEnvironment,
  setEnvironmentIdleSuspend,
} from "./client";

const id = "b351b316-fdc4-4f50-a642-758992ae3323";
const target = create(EnvironmentTargetSchema, { namespace: "team", name: "test", uid: "uid" });
afterEach(() => vi.unstubAllGlobals());

it("sends the generated exact target through the EnvironmentService facade", async () => {
  const target = create(EnvironmentTargetSchema, { namespace: "team", name: "box", uid: "opaque-uid" });
  const fetchMock = vi
    .fn()
    .mockResolvedValueOnce(Response.json({}))
    .mockResolvedValueOnce(
      Response.json({
        target: { namespace: "team", name: "box", uid: "opaque-uid" },
        outcome: "RESUME_OUTCOME_REQUESTED",
      })
    )
    .mockResolvedValueOnce(Response.json({}))
    .mockResolvedValueOnce(Response.json({}));
  vi.stubGlobal("fetch", fetchMock);
  await suspendEnvironment(target);
  await resumeSandbox(target);
  await setEnvironmentIdleSuspend(target, 90);
  await deleteEnvironment(target);
  expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
    "/upstream/environments/v1/environments/team/box:suspend",
    "/upstream/environments/v1/environments/team/box:resume",
    "/upstream/environments/v1/environments/team/box:idle-suspend",
    "/upstream/environments/v1/environments/team/box?target.uid=opaque-uid",
  ]);
  expect(JSON.parse(fetchMock.mock.calls[0]![1].body)).toEqual({
    target: { namespace: "team", name: "box", uid: "opaque-uid" },
  });
  expect(JSON.parse(fetchMock.mock.calls[2]![1].body)).toEqual({
    target: { namespace: "team", name: "box", uid: "opaque-uid" },
    idleSuspendAfter: "90s",
  });
});

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
      target,
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
    .mockResolvedValueOnce(
      Response.json({
        opening: { sessionId: id, sandbox: "test", state: "DELIVERY_STATE_RUNNER_ADMITTED" },
      })
    );
  vi.stubGlobal("fetch", fetchMock);
  await expect(openSession(target, id, create(SessionSpecSchema))).rejects.toMatchObject({ code: 4 });
  const opening = await getSessionOpening(id);
  expect(opening.state).toBe(DeliveryState.RUNNER_ADMITTED);
  expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
    "/sandboxes/test/sessions",
    `/upstream/dispatcher/v1/sessions/${id}/opening`,
  ]);
});

it("rejects an opening for a different Session ID", async () => {
  const fetchMock = vi
    .fn()
    .mockResolvedValue(Response.json({ opening: { sessionId: "different-session", sandbox: "test" } }));
  vi.stubGlobal("fetch", fetchMock);

  await expect(getSessionOpening(id)).rejects.toThrow(
    `Dispatcher returned Session different-session for requested Session ${id}`
  );
  expect(fetchMock).toHaveBeenCalledTimes(1);
  expect(fetchMock.mock.calls[0]![0]).toBe(`/upstream/dispatcher/v1/sessions/${id}/opening`);
});

it("reads the AppService runner status as the existing generated EnvironmentRuntimeStatus", async () => {
  const fetchMock = vi.fn().mockResolvedValue(
    Response.json({
      environmentRuntimeBootId: "fixture-boot",
      processes: [],
      sessions: [],
      lifecycle: {
        desiredMode: "ENVIRONMENT_RUNTIME_LIFECYCLE_MODE_QUIESCING",
        desiredGeneration: "9223372036854775809",
        progressGeneration: "7",
        progressAvailable: true,
        progressCurrent: false,
        stage: "ENVIRONMENT_RUNTIME_LIFECYCLE_STAGE_FLUSHING",
        error: "",
        blocker: {
          kind: "ENVIRONMENT_RUNTIME_LIFECYCLE_BLOCKER_KIND_INGRESS_ACKNOWLEDGEMENT",
          error: "",
          resourceType: "",
          resourceId: "",
          processId: "",
          resourcePresent: false,
          admissions: "0",
          journalSealPresent: false,
          journalSealUid: "",
          expectedJournalSealUid: "",
          journalSealCursor: "9223372036854775811",
          acknowledgementPresent: false,
          acknowledgementSealed: false,
          acknowledgementCursor: "9223372036854775810",
          finalizersPending: false,
        },
      },
    })
  );
  vi.stubGlobal("fetch", fetchMock);

  const statusTarget = create(EnvironmentTargetSchema, { namespace: "team", name: "test", uid: "sandbox-uid" });
  const status = await getEnvironmentRuntimeStatus(statusTarget);

  expect(fetchMock).toHaveBeenCalledTimes(1);
  const [url, init] = fetchMock.mock.calls[0]!;
  expect(url).toBe("/sandboxes/test/runner-status?sandbox_uid=sandbox-uid");
  expect(new Headers(init.headers).get("Accept")).toBe("application/json");
  expect(status.environmentRuntimeBootId).toBe("fixture-boot");
  expect(status.lifecycle?.desiredMode).toBe(EnvironmentRuntimeLifecycleMode.QUIESCING);
  expect(status.lifecycle?.desiredGeneration).toBe(9223372036854775809n);
  expect(status.lifecycle?.blocker?.kind).toBe(EnvironmentRuntimeLifecycleBlockerKind.INGRESS_ACKNOWLEDGEMENT);
  expect(status.lifecycle?.blocker?.journalSealCursor).toBe(9223372036854775811n);
  expect(status.processes).toEqual([]);
  expect(status.sessions).toEqual([]);
});

it("reads snake_case status from an older app replica during rollout", async () => {
  const fetchMock = vi.fn().mockResolvedValue(
    Response.json({
      environment_runtime_boot_id: "old-replica-boot",
      lifecycle: {
        desired_mode: "ENVIRONMENT_RUNTIME_LIFECYCLE_MODE_QUIESCING",
        desired_generation: "4",
        progress_generation: "4",
        progress_available: true,
        progress_current: true,
        stage: "ENVIRONMENT_RUNTIME_LIFECYCLE_STAGE_FLUSHING",
      },
      processes: [],
      sessions: [],
    })
  );
  vi.stubGlobal("fetch", fetchMock);

  const statusTarget = create(EnvironmentTargetSchema, { namespace: "team", name: "test", uid: "sandbox-uid" });
  const status = await getEnvironmentRuntimeStatus(statusTarget);

  expect(status.environmentRuntimeBootId).toBe("old-replica-boot");
  expect(status.lifecycle?.desiredGeneration).toBe(4n);
});

it("keeps runner status errors readable across old and new app replicas", async () => {
  const fetchMock = vi
    .fn()
    .mockResolvedValueOnce(Response.json({ detail: "Runner is unavailable" }, { status: 503 }))
    .mockResolvedValueOnce(Response.json({ code: 14, message: "Runner is unavailable" }, { status: 503 }));
  vi.stubGlobal("fetch", fetchMock);

  const statusTarget = create(EnvironmentTargetSchema, { namespace: "team", name: "test", uid: "sandbox-uid" });
  await expect(getEnvironmentRuntimeStatus(statusTarget)).rejects.toMatchObject({ status: 503 });
  await expect(getEnvironmentRuntimeStatus(statusTarget)).rejects.toMatchObject({ status: 503, code: 14 });
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

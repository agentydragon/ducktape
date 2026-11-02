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
import { TargetHandleSchema } from "../../../sandboxes/environments/environment_service_pb";
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
afterEach(() => vi.unstubAllGlobals());

it("sends the generated exact target through the EnvironmentService facade", async () => {
  const target = create(TargetHandleSchema, { namespace: "team", name: "box", uid: "opaque-uid" });
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

it("reads the AppService runner status as the existing generated EnvironmentRuntimeStatus", async () => {
  const fetchMock = vi.fn().mockResolvedValue(
    Response.json({
      environment_runtime_boot_id: "fixture-boot",
      lifecycle: {
        desired_mode: "ENVIRONMENT_RUNTIME_LIFECYCLE_MODE_QUIESCING",
        desired_generation: "9223372036854775809",
        progress_generation: "7",
        progress_available: true,
        progress_current: false,
        stage: "ENVIRONMENT_RUNTIME_LIFECYCLE_STAGE_FLUSHING",
        blocker: {
          kind: "ENVIRONMENT_RUNTIME_LIFECYCLE_BLOCKER_KIND_INGRESS_ACKNOWLEDGEMENT",
          journal_seal_cursor: "9223372036854775811",
          acknowledgement_cursor: "9223372036854775810",
        },
      },
    })
  );
  vi.stubGlobal("fetch", fetchMock);

  const status = await getEnvironmentRuntimeStatus("test", "sandbox-uid");

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
});

it("keeps runner status errors readable across old and generated AppService responses", async () => {
  const fetchMock = vi
    .fn()
    .mockResolvedValueOnce(Response.json({ detail: "Runner is unavailable" }, { status: 503 }))
    .mockResolvedValueOnce(Response.json({ code: 14, message: "Runner is unavailable" }, { status: 503 }));
  vi.stubGlobal("fetch", fetchMock);

  await expect(getEnvironmentRuntimeStatus("test", "sandbox-uid")).rejects.toMatchObject({ status: 503 });
  await expect(getEnvironmentRuntimeStatus("test", "sandbox-uid")).rejects.toMatchObject({ status: 503, code: 14 });
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

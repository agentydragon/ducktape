import { create } from "@bufbuild/protobuf";
import { afterEach, expect, it, vi } from "vitest";

import { EnvironmentRuntimeLifecycleBlockerKind, EnvironmentRuntimeLifecycleMode } from "../../../proto/session_pb";
import { EnvironmentTargetSchema } from "../../../proto/identity_pb";
import { CreateEnvironmentRequestSchema } from "../../../environments/v1/environment_service_pb";

import {
  launchEnvironment,
  getRuntimeDiagnostics,
  resumeSandbox,
  suspendEnvironment,
  deleteEnvironment,
  getEnvironmentIdentityRecord,
  setEnvironmentIdentity,
  setEnvironmentIdleSuspend,
} from "./client";

afterEach(() => vi.unstubAllGlobals());

it("launches through the operator APIs with the exact picked PolicySets and Identity", async () => {
  const request = create(CreateEnvironmentRequestSchema, {
    identity: "launched",
    name: "launched",
    template: "test-template",
    preset: "test-preset",
    inboxTags: ["test-group"],
  });
  const fetchMock = vi
    .fn()
    .mockResolvedValueOnce(
      Response.json({ identity: { identity: { namespace: "policies", name: "launched", uid: "identity-uid" } } })
    )
    .mockResolvedValueOnce(Response.json({ binding: {} }))
    .mockResolvedValueOnce(Response.json({ namespace: "team", name: "launched", uid: "sandbox-uid" }));
  vi.stubGlobal("fetch", fetchMock);

  await expect(launchEnvironment(request, ["test-basic", "test-packages"])).resolves.toEqual(
    create(EnvironmentTargetSchema, { namespace: "team", name: "launched", uid: "sandbox-uid" })
  );

  expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
    "/upstream/policy/v1/identities",
    "/upstream/policy/v1/identities/launched/policy-bindings",
    "/upstream/environments/v1/environments",
  ]);
  expect(JSON.parse(fetchMock.mock.calls[0]![1].body)).toEqual({
    name: "launched",
    description: "Agent of Sandbox launched, launched from the app.",
  });
  expect(JSON.parse(fetchMock.mock.calls[1]![1].body)).toEqual({
    identityUid: "identity-uid",
    policySets: ["test-basic", "test-packages"],
  });
  expect(JSON.parse(fetchMock.mock.calls[2]![1].body)).toEqual({
    name: "launched",
    template: "test-template",
    preset: "test-preset",
    inboxTags: ["test-group"],
    identity: "launched",
  });
});

it("leaves the Identity when the Environment create outcome is uncertain", async () => {
  const request = create(CreateEnvironmentRequestSchema, {
    identity: "uncertain",
    name: "uncertain",
    template: "test-template",
  });
  const fetchMock = vi
    .fn()
    .mockResolvedValueOnce(
      Response.json({ identity: { identity: { namespace: "policies", name: "uncertain", uid: "identity-uid" } } })
    )
    .mockResolvedValueOnce(Response.json({ binding: {} }))
    .mockResolvedValueOnce(Response.json({ code: 4, message: "unknown result" }, { status: 504 }));
  vi.stubGlobal("fetch", fetchMock);

  await expect(launchEnvironment(request, ["test-basic"])).rejects.toMatchObject({ code: 4 });
  expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
    "/upstream/policy/v1/identities",
    "/upstream/policy/v1/identities/uncertain/policy-bindings",
    "/upstream/environments/v1/environments",
  ]);
});

it.each([408, 429])("keeps the Identity after an Environment create response with status %i", async (status) => {
  const request = create(CreateEnvironmentRequestSchema, {
    identity: `uncertain-${status}`,
    name: `uncertain-${status}`,
    template: "test-template",
  });
  const fetchMock = vi
    .fn()
    .mockResolvedValueOnce(
      Response.json({
        identity: { identity: { namespace: "policies", name: request.name, uid: "identity-uid" } },
      })
    )
    .mockResolvedValueOnce(Response.json({ binding: {} }))
    .mockResolvedValueOnce(Response.json({ code: 4, message: "unknown result" }, { status }));
  vi.stubGlobal("fetch", fetchMock);

  await expect(launchEnvironment(request, ["test-basic"])).rejects.toMatchObject({ code: 4 });
  expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
    "/upstream/policy/v1/identities",
    `/upstream/policy/v1/identities/${request.name}/policy-bindings`,
    "/upstream/environments/v1/environments",
  ]);
});

it("deletes the Identity after a definite Environment create refusal", async () => {
  const request = create(CreateEnvironmentRequestSchema, {
    identity: "rejected",
    name: "rejected",
    template: "test-template",
  });
  const fetchMock = vi
    .fn()
    .mockResolvedValueOnce(
      Response.json({ identity: { identity: { namespace: "policies", name: "rejected", uid: "identity-uid" } } })
    )
    .mockResolvedValueOnce(Response.json({ binding: {} }))
    .mockResolvedValueOnce(Response.json({ code: 3, message: "invalid request" }, { status: 400 }))
    .mockResolvedValueOnce(Response.json({}));
  vi.stubGlobal("fetch", fetchMock);

  await expect(launchEnvironment(request, ["test-basic"])).rejects.toMatchObject({ code: 3 });
  expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
    "/upstream/policy/v1/identities",
    "/upstream/policy/v1/identities/rejected/policy-bindings",
    "/upstream/environments/v1/environments",
    "/upstream/policy/v1/identities/policies/rejected?uid=identity-uid",
  ]);
  expect(fetchMock.mock.calls[3]![1].method).toBe("DELETE");
});

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

it("reads the current credential and patches an exact stopped Environment identity", async () => {
  const exact = create(EnvironmentTargetSchema, { namespace: "team", name: "box", uid: "opaque-uid" });
  const fetchMock = vi
    .fn()
    .mockResolvedValueOnce(Response.json({ identity: { name: "prior", uid: "prior-uid" } }))
    .mockResolvedValueOnce(Response.json({}))
    .mockResolvedValueOnce(Response.json({}));
  vi.stubGlobal("fetch", fetchMock);
  await expect(getEnvironmentIdentityRecord(exact)).resolves.toBe("prior");
  await setEnvironmentIdentity(exact, "next");
  await setEnvironmentIdentity(exact, null);
  expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
    "/upstream/environments/v1/environments/team/box:identity-record?target.uid=opaque-uid",
    "/upstream/environments/v1/environments/team/box:identity",
    "/upstream/environments/v1/environments/team/box:identity",
  ]);
  expect(fetchMock.mock.calls[1]![1].method).toBe("PATCH");
  expect(JSON.parse(fetchMock.mock.calls[1]![1].body)).toEqual({
    target: { namespace: "team", name: "box", uid: "opaque-uid" },
    identityName: "next",
  });
  expect(JSON.parse(fetchMock.mock.calls[2]![1].body)).toEqual({
    target: { namespace: "team", name: "box", uid: "opaque-uid" },
  });
});

it("reads exact-target Runtime diagnostics and preserves lifecycle cursors", async () => {
  const fetchMock = vi.fn().mockResolvedValue(
    Response.json({
      target: { namespace: "team", name: "test", uid: "sandbox-uid" },
      incarnationUid: "fixture-incarnation",
      observedAt: "2026-01-01T00:00:00Z",
      status: {
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
      },
    })
  );
  vi.stubGlobal("fetch", fetchMock);

  const statusTarget = create(EnvironmentTargetSchema, { namespace: "team", name: "test", uid: "sandbox-uid" });
  const status = await getRuntimeDiagnostics(statusTarget);

  expect(fetchMock).toHaveBeenCalledTimes(1);
  const [url, init] = fetchMock.mock.calls[0]!;
  expect(url).toBe("/upstream/environments/v1/environments/team/test:runtime-diagnostics?target.uid=sandbox-uid");
  expect(new Headers(init.headers).get("Accept")).toBe("application/json");
  expect(status.environmentRuntimeBootId).toBe("fixture-boot");
  expect(status.lifecycle?.desiredMode).toBe(EnvironmentRuntimeLifecycleMode.QUIESCING);
  expect(status.lifecycle?.desiredGeneration).toBe(9223372036854775809n);
  expect(status.lifecycle?.blocker?.kind).toBe(EnvironmentRuntimeLifecycleBlockerKind.INGRESS_ACKNOWLEDGEMENT);
  expect(status.lifecycle?.blocker?.journalSealCursor).toBe(9223372036854775811n);
  expect(status.processes).toEqual([]);
});

it.each([
  { label: "namespace", target: { namespace: "other", name: "test", uid: "sandbox-uid" } },
  { label: "name", target: { namespace: "team", name: "other", uid: "sandbox-uid" } },
  { label: "UID", target: { namespace: "team", name: "test", uid: "replacement-uid" } },
  { label: "missing target", target: undefined },
  { label: "missing incarnation", incarnationUid: "" },
  { label: "missing observation", observedAt: undefined },
  { label: "invalid observation", observedAt: "not-a-timestamp" },
  { label: "missing boot ID", status: {} },
])("refuses mismatched or incomplete Runtime evidence: $label", async ({ label, ...invalid }) => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      Response.json({
        target: { namespace: "team", name: "test", uid: "sandbox-uid" },
        incarnationUid: "fixture-incarnation",
        observedAt: "2026-01-01T00:00:00Z",
        status: { environmentRuntimeBootId: "fixture-boot" },
        ...invalid,
      })
    )
  );
  await expect(
    getRuntimeDiagnostics(
      create(EnvironmentTargetSchema, {
        namespace: "team",
        name: "test",
        uid: "sandbox-uid",
      })
    ),
    label
  ).rejects.toThrow();
});

it.each(["namespace", "name", "uid"] as const)(
  "requires the target %s before requesting diagnostics",
  async (field) => {
    const target = create(EnvironmentTargetSchema, { namespace: "team", name: "test", uid: "sandbox-uid" });
    target[field] = "";
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    await expect(getRuntimeDiagnostics(target)).rejects.toThrow();
    expect(fetchMock).not.toHaveBeenCalled();
  }
);

it("keeps proxy and backend diagnostics failures readable", async () => {
  const fetchMock = vi
    .fn()
    .mockResolvedValueOnce(Response.json({ detail: "Runner is unavailable" }, { status: 503 }))
    .mockResolvedValueOnce(Response.json({ code: 14, message: "Runner is unavailable" }, { status: 503 }));
  vi.stubGlobal("fetch", fetchMock);

  const statusTarget = create(EnvironmentTargetSchema, { namespace: "team", name: "test", uid: "sandbox-uid" });
  await expect(getRuntimeDiagnostics(statusTarget)).rejects.toMatchObject({ status: 503 });
  await expect(getRuntimeDiagnostics(statusTarget)).rejects.toMatchObject({ status: 503, code: 14 });
});

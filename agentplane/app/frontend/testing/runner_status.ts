import { create, toJson, type JsonObject, type MessageInitShape } from "@bufbuild/protobuf";
import { timestampFromDate } from "@bufbuild/protobuf/wkt";

import { GetRuntimeDiagnosticsResponseSchema } from "../../../environments/v1/environment_service_pb";
import type { EnvironmentTarget } from "../../../proto/identity_pb";

import {
  EnvironmentRuntimeLifecycleBlockerKind,
  EnvironmentRuntimeLifecycleMode,
  EnvironmentRuntimeLifecycleStage,
  type EnvironmentRuntimeLifecycleStatusSchema,
  EnvironmentRuntimeStatusSchema,
} from "../../../proto/session_pb";

type AcknowledgedQuiescingLifecycle = {
  generation: bigint;
  sessionId: string;
  journalSealUid: string;
  journalSealCursor: bigint;
  acknowledgementCursor: bigint;
};

export function acknowledgedQuiescingLifecycleFixture(
  fixture: AcknowledgedQuiescingLifecycle
): MessageInitShape<typeof EnvironmentRuntimeLifecycleStatusSchema> {
  return {
    desiredMode: EnvironmentRuntimeLifecycleMode.QUIESCING,
    desiredGeneration: fixture.generation,
    progressGeneration: fixture.generation,
    progressAvailable: true,
    progressCurrent: true,
    stage: EnvironmentRuntimeLifecycleStage.FLUSHING,
    error: "",
    blocker: {
      kind: EnvironmentRuntimeLifecycleBlockerKind.INGRESS_ACKNOWLEDGEMENT,
      error: "",
      resourceType: "IngressAcknowledgements.agentplane",
      resourceId: fixture.sessionId,
      processId: "",
      resourcePresent: true,
      admissions: 0n,
      journalSealPresent: true,
      journalSealUid: fixture.journalSealUid,
      expectedJournalSealUid: fixture.journalSealUid,
      journalSealCursor: fixture.journalSealCursor,
      acknowledgementPresent: true,
      acknowledgementSealed: true,
      acknowledgementCursor: fixture.acknowledgementCursor,
      finalizersPending: false,
    },
  };
}

export function runnerStatusFixture(
  target: EnvironmentTarget,
  status: MessageInitShape<typeof EnvironmentRuntimeStatusSchema>
): JsonObject {
  return toJson(
    GetRuntimeDiagnosticsResponseSchema,
    create(GetRuntimeDiagnosticsResponseSchema, {
      target,
      incarnationUid: "fixture-incarnation",
      observedAt: timestampFromDate(new Date("2026-01-01T00:00:00Z")),
      status: create(EnvironmentRuntimeStatusSchema, status),
    }),
    { alwaysEmitImplicit: true, useProtoFieldName: true }
  ) as JsonObject;
}

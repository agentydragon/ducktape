import { Harness, type Attached } from "../../proto/session_pb";

/** Read-only harness projection; the SessionSpec placement remains the live backend authority. */
export function attachedHarness(attached: Attached | undefined): Harness | undefined {
  switch (attached?.spec?.placement.case) {
    case "environmentHosted":
      return attached.spec.placement.value.harness;
    case "centralSamplerV1":
      return Harness.CENTRAL_SAMPLER;
    default:
      return attached?.historicalHarness === Harness.UNSPECIFIED ? undefined : attached?.historicalHarness;
  }
}

interface HarnessBehavior {
  commandsRequireReadySandbox: boolean;
  llmTraceScope: "sandbox" | "session";
}

export function harnessBehavior(harness: Harness | undefined): HarnessBehavior {
  switch (harness) {
    case Harness.CENTRAL_SAMPLER:
      return { commandsRequireReadySandbox: false, llmTraceScope: "session" };
    case Harness.CODEX:
    case Harness.CLAUDE:
    case Harness.UNSPECIFIED:
    case undefined:
      return { commandsRequireReadySandbox: true, llmTraceScope: "sandbox" };
  }
}

import { Harness } from "../../proto/session_pb";

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

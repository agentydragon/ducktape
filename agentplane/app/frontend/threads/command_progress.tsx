import IconCheck from "@tabler/icons-react/dist/esm/icons/IconCheck.mjs";
import { type JSX, useState } from "react";

/** Only durable evidence advances the lights; a lost reply is not a runner refusal. */
export type CommandStage = "local" | "unconfirmed" | "admitted" | "effected" | "failed" | "noop" | "refused";
export type CommandSubject = "input" | "model" | "effort" | "other";

type LightState = "done" | "waiting" | "uncertain" | "failed" | "future";

export function CommandProgress({
  stage,
  subject,
  reason,
  local = false,
  unsent = false,
}: {
  stage: Exclude<CommandStage, "noop">;
  subject: CommandSubject;
  reason?: string | null;
  /** This browser actually retained the command; server-only rows cannot claim that. */
  local?: boolean;
  /** Only the browser owns a command that has not been attempted over HTTP yet. */
  unsent?: boolean;
}): JSX.Element {
  const [touchOpen, setTouchOpen] = useState(false);
  const waitingFor =
    subject === "input"
      ? "agent confirmation"
      : subject === "model"
        ? "model change"
        : subject === "effort"
          ? "effort change"
          : "result";
  const labels: Record<Exclude<CommandStage, "noop">, string> = {
    local: "Saved in browser · waiting for runner to accept",
    unconfirmed: "Runner receipt unconfirmed",
    admitted: `Runner accepted · waiting for ${waitingFor}`,
    effected: subject === "input" ? "Agent confirmed message" : "Applied",
    failed: "Failed",
    refused: "Command refused",
  };
  const description =
    stage === "local" && !local
      ? "Waiting for runner to accept"
      : stage === "local" && unsent
        ? "Saved in browser · waiting for connection to send"
        : labels[stage];
  const label = `${description}${reason ? `: ${reason}` : ""}`;
  // The first light means submitted, not proof that another browser saved it locally.
  const current = stage === "local" || stage === "unconfirmed" || stage === "refused" ? 2 : 3;
  const lights: LightState[] = [1, 2, 3].map((step) =>
    step < current
      ? "done"
      : step > current
        ? "future"
        : stage === "failed" || stage === "refused"
          ? "failed"
          : stage === "unconfirmed"
            ? "uncertain"
            : "waiting"
  );
  return (
    <span
      className="agentplane-command-progress"
      data-stage={stage}
      data-touch-open={touchOpen || undefined}
      role={stage === "failed" || stage === "refused" ? "alert" : "status"}
      aria-label={label}
    >
      <button
        type="button"
        className="agentplane-command-progress-hit"
        aria-label={label}
        onPointerDown={(event) => {
          if (event.pointerType !== "mouse") setTouchOpen((open) => !open);
        }}
        onBlur={() => setTouchOpen(false)}
        onKeyDown={(event) => {
          if (event.key === "Escape") setTouchOpen(false);
        }}
      >
        {stage === "effected" ? (
          <IconCheck className="agentplane-command-check" size={16} aria-hidden="true" />
        ) : (
          <span className="agentplane-command-lights" aria-hidden="true">
            {lights.map((state, index) => (
              <span key={index} className="agentplane-command-light" data-state={state} />
            ))}
          </span>
        )}
      </button>
      <span className="agentplane-command-progress-label" role="tooltip">
        {label}
      </span>
    </span>
  );
}

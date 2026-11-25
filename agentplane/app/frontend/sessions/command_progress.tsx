import { FocusTrap, Popover, Stack, Text } from "@mantine/core";
import { IconCheck } from "../ui/icons";
import { type JSX, type PointerEvent, type ReactNode, useEffect, useRef, useState } from "react";

/** Only durable evidence advances the lights; a lost reply is not a runner refusal. */
export type CommandStage =
  | "local"
  | "unconfirmed"
  | "queued"
  | "delivering"
  | "admitted"
  | "effected"
  | "failed"
  | "dispatch_failed"
  | "cancelled"
  | "unknown"
  | "delivery_error_unknown"
  | "admitted_unknown"
  | "noop"
  | "refused";
export type CommandSubject = "input" | "model" | "effort" | "other";

type LightState = "done" | "waiting" | "uncertain" | "failed" | "future";

/** Submission, Dispatcher delivery, runner admission, and observed effect, in that order. */
const stageLights: Record<CommandStage, readonly [step: number, state: LightState]> = {
  local: [1, "waiting"],
  unconfirmed: [1, "uncertain"],
  refused: [1, "failed"],
  queued: [2, "waiting"],
  delivering: [2, "waiting"],
  dispatch_failed: [2, "failed"],
  cancelled: [2, "future"],
  unknown: [2, "uncertain"],
  delivery_error_unknown: [2, "failed"],
  admitted: [4, "waiting"],
  admitted_unknown: [4, "uncertain"],
  failed: [4, "failed"],
  effected: [4, "done"],
  noop: [4, "done"],
};

/** deliveryFailure records non-retryable gRPC codes as "runner <Code>: <message>".
 * The error can be local to the delivery router or at its remote destination. It says
 * that attempt errored, without proving that the runner rejected the original command. */
const deliveryAttemptError =
  /^runner (?:Canceled|Unknown|InvalidArgument|NotFound|AlreadyExists|PermissionDenied|ResourceExhausted|FailedPrecondition|OutOfRange|Unimplemented|Internal|DataLoss|Unauthenticated): /;

export function unknownDeliveryStage(
  reason: string | undefined,
  admitted: boolean
): "unknown" | "delivery_error_unknown" | "admitted_unknown" {
  if (admitted) return "admitted_unknown";
  return reason && deliveryAttemptError.test(reason) ? "delivery_error_unknown" : "unknown";
}

/** Reasons the Dispatcher gives as `<reason>: <detail>`, where the detail names the failure it met. */
export function waitingReasonLabel(reason: string | null | undefined): string | null | undefined {
  const detailOf = (prefix: string): string | null =>
    reason?.startsWith(`${prefix}:`) ? reason.slice(prefix.length + 1).trim() : null;
  const deadline = detailOf("wake_deadline_expired");
  if (deadline !== null) return `Sandbox did not become Ready within 10 minutes: ${deadline}`;
  const wake = detailOf("sandbox_wake_failed");
  if (wake !== null) return `Dispatcher could not wake the Sandbox through Kubernetes and keeps retrying: ${wake}`;
  const runner = detailOf("runner_unreachable");
  if (runner !== null) return `Dispatcher could not reach the runner and keeps retrying: ${runner}`;
  switch (reason) {
    case "sandbox_resuming":
      return "Sandbox is waking; message is queued";
    case "wake_on_send_disabled":
      return "Message is queued; wake on message is off, so resume the Sandbox manually to send it";
    case "sandbox_suspended":
      return "Sandbox is suspended; message is queued until someone resumes it";
    case "sandbox_not_ready":
      return "Sandbox is not ready; message is queued";
    case "runner_refused_work":
      return "Waiting for Sandbox to resume";
    default:
      return reason;
  }
}

export function CommandProgress({
  stage,
  subject,
  reason,
  details,
  local = false,
  unsent = false,
}: {
  stage: CommandStage;
  subject: CommandSubject;
  reason?: string | null;
  details?: ReactNode;
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
  const labels: Record<CommandStage, string> = {
    local: "Saved in browser · waiting for Dispatcher",
    unconfirmed: "Dispatcher receipt unconfirmed",
    queued: "Queued in Dispatcher · waiting for runner",
    delivering: "Dispatcher is sending to runner",
    admitted: `Runner accepted · waiting for ${waitingFor}`,
    effected: subject === "input" ? "Agent confirmed message" : "Applied",
    failed: "Execution failed",
    dispatch_failed: "Delivery failed",
    cancelled: "Cancelled before delivery",
    unknown: "Outcome unknown",
    delivery_error_unknown: "Outcome unknown",
    admitted_unknown: "Outcome unknown",
    noop: "No-op",
    refused: "Submission refused",
  };
  const reasonLabel = waitingReasonLabel(reason);
  const label =
    stage === "local" && !local
      ? "Waiting for Dispatcher"
      : stage === "local" && unsent
        ? "Saved in browser · waiting for connection to send"
        : stage === "queued" && reasonLabel
          ? reasonLabel
          : `${labels[stage]}${reasonLabel ? `: ${reasonLabel}` : ""}`;
  const terminalFailure = stage === "failed" || stage === "dispatch_failed" || stage === "refused";
  const deliveryError = stage === "delivery_error_unknown";
  const [current, light] = stageLights[stage];
  const lights: LightState[] = [1, 2, 3, 4].map((step) =>
    step < current ? "done" : step > current ? "future" : light
  );
  const indicator =
    stage === "noop" ? null : stage === "effected" ? (
      <IconCheck className="agentplane-command-check" size={16} aria-hidden="true" />
    ) : (
      <span className="agentplane-command-lights" aria-hidden="true">
        {lights.map((state, index) => (
          <span key={index} className="agentplane-command-light" data-state={state} />
        ))}
      </span>
    );
  if (
    terminalFailure ||
    deliveryError ||
    light === "uncertain" ||
    stage === "noop" ||
    stage === "cancelled" ||
    details
  ) {
    const summary = stage === "queued" ? "Queued" : stage === "delivering" ? "Sending" : labels[stage];
    return (
      <span
        className={`agentplane-command-progress-inline ${stage === "noop" ? "agentplane-command-noop" : "agentplane-command-progress"}`}
        data-stage={stage}
        role={terminalFailure ? "alert" : "status"}
        aria-label={deliveryError ? "Outcome unknown · latest delivery attempt returned an error" : undefined}
      >
        {indicator}
        {reasonLabel || details ? (
          <CommandDetails summary={summary} reason={reasonLabel}>
            {details}
          </CommandDetails>
        ) : (
          <span>{summary}</span>
        )}
      </span>
    );
  }
  return (
    <span
      className="agentplane-command-progress"
      data-stage={stage}
      data-touch-open={touchOpen || undefined}
      role="status"
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
        {indicator}
      </button>
      <span className="agentplane-command-progress-label" role="tooltip">
        {label}
      </span>
    </span>
  );
}

function CommandDetails({
  summary,
  reason,
  children,
}: {
  summary: string;
  reason: string | null | undefined;
  children: ReactNode;
}): JSX.Element {
  const [mode, setMode] = useState<"hover" | "activated" | null>(null);
  const timer = useRef<number | undefined>(undefined);
  useEffect(() => () => window.clearTimeout(timer.current), []);
  useEffect(() => {
    if (mode !== "hover") return;
    const dismissPreview = (event: KeyboardEvent) => {
      if (event.key === "Escape") setMode(null);
    };
    window.addEventListener("keydown", dismissPreview);
    return () => window.removeEventListener("keydown", dismissPreview);
  }, [mode]);
  const preview = (event: PointerEvent) => {
    if (event.pointerType !== "mouse" || !window.matchMedia("(any-hover: hover)").matches) return;
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setMode((open) => open ?? "hover"), 200);
  };
  const leave = (event: PointerEvent) => {
    if (event.pointerType !== "mouse") return;
    window.clearTimeout(timer.current);
    // Allow the pointer to cross the gap from the status to its popover.
    timer.current = window.setTimeout(() => setMode((open) => (open === "hover" ? null : open)), 250);
  };
  const activate = () => {
    window.clearTimeout(timer.current);
    setMode((open) => (open === "activated" ? null : "activated"));
  };

  return (
    <Popover
      position="bottom-start"
      shadow="md"
      width="min(22rem, calc(100vw - 2rem))"
      opened={mode !== null}
      onDismiss={() => {
        window.clearTimeout(timer.current);
        setMode(null);
      }}
      trapFocus={mode === "activated"}
      returnFocus={mode === "activated"}
    >
      <Popover.Target>
        <button
          type="button"
          className="agentplane-command-details-trigger"
          aria-label={summary + " · delivery details"}
          onPointerEnter={preview}
          onPointerLeave={leave}
          onClick={activate}
        >
          {summary}
        </button>
      </Popover.Target>
      <Popover.Dropdown
        aria-label="Delivery details"
        onPointerEnter={() => window.clearTimeout(timer.current)}
        onPointerLeave={leave}
      >
        <FocusTrap.InitialFocus />
        <Stack gap="xs">
          {reason && (
            <Text size="sm" style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
              {reason}
            </Text>
          )}
          {children}
        </Stack>
      </Popover.Dropdown>
    </Popover>
  );
}

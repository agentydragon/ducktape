import { Badge, Button, Group, Stack, Text, Tooltip, UnstyledButton } from "@mantine/core";
import IconArrowRight from "@tabler/icons-react/dist/esm/icons/IconArrowRight.mjs";
import IconChevronDown from "@tabler/icons-react/dist/esm/icons/IconChevronDown.mjs";
import IconChevronRight from "@tabler/icons-react/dist/esm/icons/IconChevronRight.mjs";
import { type JSX, useContext, useEffect, useMemo, useRef, useState, type PointerEvent } from "react";
import { Link, useLocation } from "react-router";

import { serviceAccountKey } from "../client";
import { StaleNotice } from "../stream_status";
import { canApproveInline, compactActionArguments, renderArguments } from "./rendering/index";
import { ActionRequestsContext } from "./requests";
import "./sidebar.css";

const MANUAL_CLOSE_KEY = "agentplane-actions-sidebar-manually-closed";
const ACTIONS_HEIGHT_KEY = "agentplane-actions-sidebar-height";
const ACTIONS_HEIGHT_DEFAULT = 336;
const ACTIONS_HEIGHT_MIN = 144;
const ACTIONS_HEIGHT_MAX = 640;
const ACTIONS_HEIGHT_KEYBOARD_STEP = 16;

function clampActionsHeight(height: number): number {
  const viewportMax = Math.max(ACTIONS_HEIGHT_MIN, Math.floor(window.innerHeight * 0.75));
  return Math.min(ACTIONS_HEIGHT_MAX, viewportMax, Math.max(ACTIONS_HEIGHT_MIN, height));
}

function readActionsHeight(): number {
  try {
    const stored = window.localStorage.getItem(ACTIONS_HEIGHT_KEY);
    const parsed = stored === null ? NaN : Number(stored);
    return Number.isFinite(parsed) ? clampActionsHeight(parsed) : clampActionsHeight(ACTIONS_HEIGHT_DEFAULT);
  } catch (reason: unknown) {
    console.warn("actions sidebar: failed to read stored height", reason);
    return clampActionsHeight(ACTIONS_HEIGHT_DEFAULT);
  }
}

function ActionsResizeHandle({
  height,
  onDrag,
  onStep,
}: {
  height: number;
  onDrag: (height: number) => void;
  onStep: (delta: number) => void;
}): JSX.Element {
  const dragRef = useRef<{ pointerId: number; startY: number; startHeight: number } | null>(null);

  function onPointerDown(event: PointerEvent<HTMLDivElement>): void {
    dragRef.current = { pointerId: event.pointerId, startY: event.clientY, startHeight: height };
    event.currentTarget.setPointerCapture?.(event.pointerId);
  }

  function onPointerMove(event: PointerEvent<HTMLDivElement>): void {
    const drag = dragRef.current;
    if (drag === null || drag.pointerId !== event.pointerId) return;
    // The handle is the panel's top edge: dragging it upward gives Actions more room.
    onDrag(drag.startHeight - (event.clientY - drag.startY));
  }

  function onPointerUp(event: PointerEvent<HTMLDivElement>): void {
    if (dragRef.current?.pointerId !== event.pointerId) return;
    dragRef.current = null;
    event.currentTarget.releasePointerCapture?.(event.pointerId);
  }

  return (
    <div
      className="agentplane-actions-sidebar-resize-handle"
      role="separator"
      aria-orientation="horizontal"
      aria-label="Resize Actions area"
      aria-controls="agentplane-actions-sidebar-content"
      aria-valuenow={height}
      aria-valuemin={ACTIONS_HEIGHT_MIN}
      aria-valuemax={Math.min(ACTIONS_HEIGHT_MAX, Math.max(ACTIONS_HEIGHT_MIN, Math.floor(window.innerHeight * 0.75)))}
      tabIndex={0}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onKeyDown={(event) => {
        if (event.key === "ArrowUp") {
          event.preventDefault();
          onStep(ACTIONS_HEIGHT_KEYBOARD_STEP);
        } else if (event.key === "ArrowDown") {
          event.preventDefault();
          onStep(-ACTIONS_HEIGHT_KEYBOARD_STEP);
        }
      }}
    />
  );
}

function readManuallyClosedRequests(): Set<string> {
  try {
    if (typeof window === "undefined") return new Set();
    const stored = window.sessionStorage.getItem(MANUAL_CLOSE_KEY);
    const parsed: unknown = stored === null ? [] : JSON.parse(stored);
    return Array.isArray(parsed) && parsed.every((item) => typeof item === "string") ? new Set(parsed) : new Set();
  } catch {
    return new Set();
  }
}

function writeManuallyClosedRequests(requestIds: readonly string[]): void {
  try {
    if (typeof window === "undefined") return;
    if (requestIds.length > 0) window.sessionStorage.setItem(MANUAL_CLOSE_KEY, JSON.stringify(requestIds));
    else window.sessionStorage.removeItem(MANUAL_CLOSE_KEY);
  } catch {
    // The choice only affects whether the sidebar expands automatically; blocked storage is harmless.
  }
}

/** Pending-only queue embedded below the thread list in the persistent navigation sidebar. */
export function ActionsSidebarSection({ onNavigate }: { onNavigate?: () => void }): JSX.Element | null {
  const actions = useContext(ActionRequestsContext);
  const location = useLocation();
  const [open, setOpen] = useState(false);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [actionsHeight, setActionsHeight] = useState(readActionsHeight);
  const manuallyClosedRequests = useRef(readManuallyClosedRequests());
  const previousIds = useRef<ReadonlySet<string>>(new Set());
  const pending = useMemo(
    () => actions?.requests.filter((request) => request.state === "decision_pending") ?? [],
    [actions?.requests]
  );
  const pendingIds = useMemo(() => pending.map((request) => request.id), [pending]);
  const actionLoading = actions?.loading ?? true;
  const actionError = actions?.error ?? null;

  function setHeight(next: number): void {
    const clamped = clampActionsHeight(next);
    setActionsHeight(clamped);
    try {
      window.localStorage.setItem(ACTIONS_HEIGHT_KEY, String(clamped));
    } catch (reason: unknown) {
      console.warn("actions sidebar: failed to persist height", reason);
    }
  }

  function resizeBy(delta: number): void {
    setActionsHeight((current) => {
      const clamped = clampActionsHeight(current + delta);
      try {
        window.localStorage.setItem(ACTIONS_HEIGHT_KEY, String(clamped));
      } catch (reason: unknown) {
        console.warn("actions sidebar: failed to persist height", reason);
      }
      return clamped;
    });
  }

  useEffect(() => {
    if (actionLoading || actionError !== null) return;
    const currentIds = new Set(pendingIds);
    if (currentIds.size === 0) {
      // A new queue is a fresh reason to expand the panel. Clearing the queue also clears a prior
      // manual-close suppression, including across reloads in this tab.
      manuallyClosedRequests.current = new Set();
      writeManuallyClosedRequests([]);
      previousIds.current = currentIds;
      setExpandedId(null);
      setOpen(false);
      return;
    }

    let autoOpenAllowed = manuallyClosedRequests.current.size === 0;
    if (!autoOpenAllowed) {
      const oldQueueStillPending = [...currentIds].some((id) => manuallyClosedRequests.current.has(id));
      if (oldQueueStillPending) {
        // Requests arriving while the old queue is still pending belong to the same queue batch.
        manuallyClosedRequests.current = new Set([...manuallyClosedRequests.current, ...currentIds]);
        writeManuallyClosedRequests([...manuallyClosedRequests.current]);
      } else {
        // The previous queue cleared while the app was closed; this is a fresh batch.
        manuallyClosedRequests.current = new Set();
        writeManuallyClosedRequests([]);
        autoOpenAllowed = true;
      }
    }
    const hasNewRequest = [...currentIds].some((id) => !previousIds.current.has(id));
    if (hasNewRequest && autoOpenAllowed) setOpen(true);
    previousIds.current = currentIds;
  }, [actionLoading, actionError, pendingIds]);

  if (actions === null) return null;

  function toggleOpen(): void {
    setOpen((wasOpen) => {
      const nextOpen = !wasOpen;
      if (!nextOpen && pending.length > 0) {
        manuallyClosedRequests.current = new Set(pendingIds);
        writeManuallyClosedRequests(pendingIds);
      } else if (nextOpen) {
        manuallyClosedRequests.current = new Set();
        writeManuallyClosedRequests([]);
      }
      return nextOpen;
    });
  }

  return (
    <section
      className={`agentplane-actions-sidebar${open ? " is-open" : ""}`}
      aria-label="Actions awaiting review"
      style={{ height: open ? `${actionsHeight}px` : undefined }}
    >
      {open && <ActionsResizeHandle height={actionsHeight} onDrag={setHeight} onStep={resizeBy} />}
      <UnstyledButton
        className="agentplane-actions-sidebar-toggle"
        onClick={toggleOpen}
        aria-expanded={open}
        aria-controls={open ? "agentplane-actions-sidebar-content" : undefined}
      >
        <Group gap="xs" wrap="nowrap">
          {open ? <IconChevronDown size={14} /> : <IconChevronRight size={14} />}
          <Text size="xs" fw={700} tt="uppercase">
            Actions
          </Text>
          {pending.length > 0 && (
            <Badge color="yellow" size="sm" circle aria-label={`${pending.length} pending actions`}>
              {pending.length}
            </Badge>
          )}
        </Group>
      </UnstyledButton>
      {open && (
        <div id="agentplane-actions-sidebar-content" className="agentplane-actions-sidebar-content">
          <StaleNotice streams={[actions.stream]} />
          {actions.error !== null && (
            <Text size="xs" c="red" role="alert">
              {actions.error}
            </Text>
          )}
          {actions.loading && (
            <Text size="xs" c="dimmed" role="status">
              Loading pending actions…
            </Text>
          )}
          {!actions.loading && actions.error === null && pending.length === 0 && (
            <Text size="xs" c="dimmed">
              No pending actions.
            </Text>
          )}
          {pending.map((request) => {
            const expanded = expandedId === request.id;
            const preview = compactActionArguments(request.action, request.arguments);
            const canQuickApprove =
              (request.external_grant === null || request.external_grant === undefined) &&
              preview !== null &&
              canApproveInline(request.action, request.arguments);
            const detailsPath = `/actions/${encodeURIComponent(request.id)}`;
            const navigationState = {
              returnTo: {
                pathname: location.pathname,
                search: location.search,
                hash: location.hash,
              },
            };

            return (
              <div className="agentplane-actions-sidebar-request" key={request.id}>
                <div className="agentplane-actions-sidebar-request-heading">
                  <UnstyledButton
                    className="agentplane-actions-sidebar-expand"
                    aria-label={`${expanded ? "Collapse" : "Expand"} ${request.title}`}
                    aria-expanded={expanded}
                    onClick={() => setExpandedId(expanded ? null : request.id)}
                  >
                    {expanded ? <IconChevronDown size={12} /> : <IconChevronRight size={12} />}
                  </UnstyledButton>
                  <div className="agentplane-actions-sidebar-request-label">
                    <Link
                      className="agentplane-actions-sidebar-name"
                      to={detailsPath}
                      state={navigationState}
                      onClick={onNavigate}
                      aria-label={`View details for ${request.action.group} / ${request.action.name}: ${request.title}`}
                    >
                      <span>
                        {request.action.group} / {request.action.name}
                      </span>
                      <IconArrowRight size={13} aria-hidden="true" />
                    </Link>
                    <Text size="xs" lineClamp={1} title={request.title}>
                      {request.title}
                    </Text>
                  </div>
                </div>
                {expanded && (
                  <Stack className="agentplane-actions-sidebar-preview" gap="xs">
                    {request.description && (
                      <Text size="xs" c="dimmed">
                        {request.description}
                      </Text>
                    )}
                    {request.caller && (
                      <Text size="xs" c="dimmed">
                        Requested by {serviceAccountKey(request.caller)}
                      </Text>
                    )}
                    {request.external_grant && (
                      <Text size="xs" fw={600}>
                        Authenticated external caller
                      </Text>
                    )}
                    {renderArguments(request.action, request.arguments) ?? preview ?? (
                      <Text size="xs" c="dimmed">
                        Open full details to inspect the action arguments.
                      </Text>
                    )}
                    <Group justify="space-between" wrap="nowrap" gap="xs">
                      {canQuickApprove ? (
                        <Button
                          size="xs"
                          loading={actions.deciding === request.id}
                          onClick={() => actions.decide(request, "allow")}
                          aria-label={`Approve ${request.title}`}
                        >
                          Approve
                        </Button>
                      ) : (
                        <Text size="xs" c="dimmed">
                          Decisions require full review.
                        </Text>
                      )}
                      <Tooltip label="View full details" withArrow>
                        <Link
                          className="agentplane-actions-sidebar-details"
                          to={detailsPath}
                          state={navigationState}
                          onClick={onNavigate}
                        >
                          Details <IconArrowRight size={13} aria-hidden="true" />
                        </Link>
                      </Tooltip>
                    </Group>
                  </Stack>
                )}
              </div>
            );
          })}
        </div>
      )}
    </section>
  );
}

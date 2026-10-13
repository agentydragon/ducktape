import { Badge, Group, Stack, Text, Tooltip, UnstyledButton } from "@mantine/core";
import IconArrowRight from "@tabler/icons-react/dist/esm/icons/IconArrowRight.mjs";
import IconChevronDown from "@tabler/icons-react/dist/esm/icons/IconChevronDown.mjs";
import IconChevronRight from "@tabler/icons-react/dist/esm/icons/IconChevronRight.mjs";
import { type JSX, useContext, useEffect, useMemo, useRef, useState } from "react";
import { Link, useLocation } from "react-router";

import { JsonView } from "../json_view";
import { ResizeHandle, usePaneSize } from "../resize_handle";
import {
  canApproveInline,
  renderActionLabel,
  renderPaneCollapsed,
  renderPaneOpened,
  shouldRenderPaneRequestTitle,
} from "./rendering/index";
import type { ActionRequestView } from "./client";
import { ActionDecisionButtons, ActionRequestsContext } from "./requests";
import "./sidebar.css";

const MANUAL_CLOSE_KEY = "agentplane-actions-sidebar-manually-closed";

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

function usePendingRequests(): ActionRequestView[] {
  const actions = useContext(ActionRequestsContext);
  return useMemo(
    () => actions?.requests.filter((request) => request.state === "decision_pending") ?? [],
    [actions?.requests]
  );
}

/** One row per pending request: expandable preview, inline decision where eligible, details link. */
function PendingActionList({
  pending,
  onNavigate,
}: {
  pending: readonly ActionRequestView[];
  onNavigate?: () => void;
}): JSX.Element | null {
  const actions = useContext(ActionRequestsContext);
  const location = useLocation();
  const [expandedId, setExpandedId] = useState<string | null>(null);
  if (actions === null) return null;
  return (
    <>
      {pending.map((request) => {
        const expanded = expandedId === request.id;
        const actionLabel = renderActionLabel(request.action, request.arguments);
        const collapsedPreview = renderPaneCollapsed(request.action, request.arguments);
        const openedPreview = renderPaneOpened(request.action, request.arguments);
        const showRequestTitle = shouldRenderPaneRequestTitle(request.action, request.arguments, request.title);
        const decision = canApproveInline(request.action, request.arguments) ? (
          <ActionDecisionButtons
            size="xs"
            deciding={actions.deciding === request.id}
            title={request.title}
            onDecide={(verdict) => actions.decide(request, verdict)}
          />
        ) : null;
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
                <div className="agentplane-actions-sidebar-request-label">
                  <div className="agentplane-actions-sidebar-name">
                    {actionLabel ?? `${request.action.group} / ${request.action.name}`}
                  </div>
                  {showRequestTitle && (
                    <Text size="xs" lineClamp={1} title={request.title}>
                      {request.title}
                    </Text>
                  )}
                  {!expanded && collapsedPreview !== null && (
                    <div className="agentplane-actions-sidebar-collapsed-preview">{collapsedPreview}</div>
                  )}
                </div>
              </UnstyledButton>
              <Tooltip label="View full details" withArrow>
                <Link
                  className="agentplane-actions-sidebar-details"
                  to={detailsPath}
                  state={navigationState}
                  onClick={onNavigate}
                  aria-label={`View details for ${request.title}`}
                >
                  <IconArrowRight size={15} aria-hidden="true" />
                </Link>
              </Tooltip>
            </div>
            {!expanded && decision !== null && <div className="agentplane-actions-sidebar-decision">{decision}</div>}
            {expanded && (
              <Stack className="agentplane-actions-sidebar-preview" gap="xs">
                {request.external_grant && (
                  <Text size="xs" fw={600}>
                    Authenticated external caller
                  </Text>
                )}
                {openedPreview ?? <JsonView value={request.arguments} />}
                <Group justify="space-between" wrap="nowrap" gap="xs">
                  {decision ?? (
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
    </>
  );
}

/** Pending-only queue embedded below the thread list in the persistent navigation sidebar. */
export function ActionsSidebarSection({ onNavigate }: { onNavigate?: () => void }): JSX.Element | null {
  const actions = useContext(ActionRequestsContext);
  const [open, setOpen] = useState(false);
  const manuallyClosedRequests = useRef(readManuallyClosedRequests());
  const previousIds = useRef<ReadonlySet<string>>(new Set());
  const pending = usePendingRequests();
  const pendingIds = useMemo(() => pending.map((request) => request.id), [pending]);
  const actionLoading = actions?.loading ?? true;
  const actionError = actions?.error ?? null;
  const pane = usePaneSize(
    "agentplane-actions-sidebar-height",
    336,
    144,
    Math.min(640, Math.max(144, Math.floor(window.innerHeight * 0.75)))
  );

  useEffect(() => {
    if (actionLoading || actionError !== null) return;
    const currentIds = new Set(pendingIds);
    if (currentIds.size === 0) {
      // A new queue is a fresh reason to expand the panel. Clearing the queue also clears a prior
      // manual-close suppression, including across reloads in this tab.
      manuallyClosedRequests.current = new Set();
      writeManuallyClosedRequests([]);
      previousIds.current = currentIds;
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
      style={{ height: open ? `${pane.size}px` : undefined }}
    >
      {open && (
        <ResizeHandle
          pane={pane}
          axis="y"
          className="agentplane-actions-sidebar-resize-handle"
          aria-label="Resize Actions area"
          aria-controls="agentplane-actions-sidebar-content"
        />
      )}
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
          <PendingActionList pending={pending} onNavigate={onNavigate} />
        </div>
      )}
    </section>
  );
}

/** The pending queue for when the navigation sidebar is closed, as at phone width: a one-line strip
    above the routed page that expands in place, in a bounded scroll area, so the open thread and
    its composer stay usable beneath it. */
export function ActionsAttentionBar(): JSX.Element | null {
  const actions = useContext(ActionRequestsContext);
  const pending = usePendingRequests();
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (pending.length === 0) setOpen(false);
  }, [pending.length]);

  if (actions === null || pending.length === 0) return null;

  return (
    <section className="agentplane-actions-attention" aria-label="Actions awaiting review">
      <UnstyledButton
        className="agentplane-actions-sidebar-toggle"
        onClick={() => setOpen((wasOpen) => !wasOpen)}
        aria-expanded={open}
        aria-controls={open ? "agentplane-actions-attention-content" : undefined}
      >
        <Group gap="xs" wrap="nowrap">
          {open ? <IconChevronDown size={14} /> : <IconChevronRight size={14} />}
          <Badge color="yellow" size="sm" circle aria-hidden="true">
            {pending.length}
          </Badge>
          <Text size="sm" fw={600}>
            {pending.length === 1 ? "Action" : "Actions"} waiting for review
          </Text>
        </Group>
      </UnstyledButton>
      {open && (
        <div id="agentplane-actions-attention-content" className="agentplane-actions-attention-content">
          {actions.error !== null && (
            <Text size="xs" c="red" role="alert">
              {actions.error}
            </Text>
          )}
          <PendingActionList pending={pending} />
        </div>
      )}
    </section>
  );
}

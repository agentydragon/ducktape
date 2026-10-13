import { Badge, Button, Group, Stack, Text, Tooltip, UnstyledButton } from "@mantine/core";
import { IconArrowRight, IconChevronDown, IconChevronRight } from "../icons";
import { type JSX, type ReactNode, useContext, useEffect, useMemo, useRef, useState } from "react";
import { Link, useLocation } from "react-router";

import { JsonBlock } from "../text_block";
import { ResizeHandle, usePaneSize } from "../../../../util/frontend/resize_handle";
import {
  actionCompactCardExpands,
  actionOffersCardDecisionButtons,
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

/** One row per pending request: expandable preview where it adds something, decision buttons where eligible,
    details link. */
function PendingActionList({
  pending,
  onNavigate,
  onOpenDetails,
}: {
  pending: readonly ActionRequestView[];
  onNavigate?: () => void;
  /** A docked workspace opens details in a pane of its own instead of navigating the route. */
  onOpenDetails?: (request: ActionRequestView) => void;
}): JSX.Element | null {
  const actions = useContext(ActionRequestsContext);
  const location = useLocation();
  const [expandedId, setExpandedId] = useState<string | null>(null);
  if (actions === null) return null;
  return (
    <>
      {pending.map((request) => {
        const expands = actionCompactCardExpands(request.action, request.arguments);
        const expanded = expands && expandedId === request.id;
        // A card without an expanded version shows all it has at once.
        const unclamped = expanded || !expands;
        const actionLabel = renderActionLabel(request.action, request.arguments);
        const collapsedPreview = renderPaneCollapsed(request.action, request.arguments);
        const openedPreview = renderPaneOpened(request.action, request.arguments);
        const showRequestTitle = shouldRenderPaneRequestTitle(request.action, request.arguments, request.title);
        const decision = actionOffersCardDecisionButtons(request.action, request.arguments) ? (
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

        const summary = (
          <div
            className={`agentplane-actions-sidebar-request-label${unclamped ? " is-unclamped" : ""}${decision === null ? "" : " offers-card-decision"}`}
          >
            <div className="agentplane-actions-sidebar-name">
              {actionLabel === undefined ? `${request.action.group} / ${request.action.name}` : actionLabel}
            </div>
            {showRequestTitle && (
              <Text size="xs" lineClamp={unclamped ? undefined : 1} title={request.title}>
                {request.title}
              </Text>
            )}
            {!expanded && collapsedPreview !== null && collapsedPreview !== undefined && (
              <div className="agentplane-actions-sidebar-collapsed-preview">{collapsedPreview}</div>
            )}
          </div>
        );

        const detailsLink = (className: string, content: ReactNode, label?: string): JSX.Element =>
          onOpenDetails === undefined ? (
            <Link
              className={className}
              to={detailsPath}
              state={navigationState}
              onClick={onNavigate}
              aria-label={label}
            >
              {content}
            </Link>
          ) : (
            <UnstyledButton className={className} onClick={() => onOpenDetails(request)} aria-label={label}>
              {content}
            </UnstyledButton>
          );

        return (
          <div className="agentplane-actions-sidebar-request" key={request.id}>
            <div className="agentplane-actions-sidebar-request-heading">
              {expands ? (
                <UnstyledButton
                  className="agentplane-actions-sidebar-summary agentplane-actions-sidebar-expand"
                  aria-label={`${expanded ? "Collapse" : "Expand"} ${request.title}`}
                  aria-expanded={expanded}
                  onClick={() => setExpandedId(expanded ? null : request.id)}
                >
                  {expanded ? <IconChevronDown size={12} /> : <IconChevronRight size={12} />}
                  {summary}
                </UnstyledButton>
              ) : (
                <div className="agentplane-actions-sidebar-summary is-static">{summary}</div>
              )}
              <Tooltip label="View full details" withArrow>
                {detailsLink(
                  "agentplane-actions-sidebar-details",
                  <IconArrowRight size={15} aria-hidden="true" />,
                  `View details for ${request.title}`
                )}
              </Tooltip>
            </div>
            {!expanded && decision !== null && <div className="agentplane-actions-sidebar-decision">{decision}</div>}
            {expanded && (
              <Stack className="agentplane-actions-sidebar-preview" gap="xs" data-operation-density="compact">
                {openedPreview === undefined ? (
                  <>
                    <Text size="xs" c="dimmed">
                      Details shown as recorded data
                    </Text>
                    <JsonBlock value={request.arguments} clamp={false} />
                  </>
                ) : (
                  openedPreview
                )}
                <Group justify="space-between" wrap="nowrap" gap="xs">
                  {decision ?? (
                    <Text size="xs" c="dimmed">
                      Decisions require full review.
                    </Text>
                  )}
                  <Tooltip label="View full details" withArrow>
                    {detailsLink(
                      "agentplane-actions-sidebar-details",
                      <>
                        Details <IconArrowRight size={13} aria-hidden="true" />
                      </>
                    )}
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

/** The pending queue with its loading, error and empty states: the body of the sidebar section and
 * of a docked Actions pane. */
export function PendingActionsQueue({
  onNavigate,
  onOpenDetails,
}: {
  onNavigate?: () => void;
  onOpenDetails?: (request: ActionRequestView) => void;
}): JSX.Element | null {
  const actions = useContext(ActionRequestsContext);
  const pending = usePendingRequests();
  if (actions === null) return null;
  return (
    <>
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
      <PendingActionList pending={pending} onNavigate={onNavigate} onOpenDetails={onOpenDetails} />
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
          <PendingActionsQueue onNavigate={onNavigate} />
        </div>
      )}
    </section>
  );
}

/** Pending requests the attention strip shows before "Show N more". */
const ATTENTION_STRIP_REQUESTS = 2;

/** The pending queue for when the navigation sidebar is closed, as at phone width: the first
    requests' compact cards above the routed page and the rest behind "Show N more", in a bounded
    scroll area, so the open thread and its composer stay usable beneath it. The shell keeps it
    mounted while `visible` is false, so the expansion survives a decision on a details page. */
export function ActionsAttentionBar({ visible }: { visible: boolean }): JSX.Element | null {
  const actions = useContext(ActionRequestsContext);
  const pending = usePendingRequests();
  const [showAll, setShowAll] = useState(false);
  const hidden = pending.length - ATTENTION_STRIP_REQUESTS;

  useEffect(() => {
    if (hidden <= 0) setShowAll(false);
  }, [hidden]);

  if (!visible || actions === null || pending.length === 0) return null;

  return (
    <section className="agentplane-actions-attention" aria-label="Actions awaiting review">
      <div id="agentplane-actions-attention-content" className="agentplane-actions-attention-content">
        {actions.error !== null && (
          <Text size="xs" c="red" role="alert">
            {actions.error}
          </Text>
        )}
        <div className="agentplane-actions-attention-cards">
          <PendingActionList pending={showAll ? pending : pending.slice(0, ATTENTION_STRIP_REQUESTS)} />
        </div>
        {hidden > 0 && (
          <Button
            className="agentplane-actions-attention-expand"
            variant="subtle"
            size="compact-xs"
            onClick={() => setShowAll((wasShown) => !wasShown)}
            aria-expanded={showAll}
            aria-controls="agentplane-actions-attention-content"
            leftSection={showAll ? <IconChevronDown size={12} /> : <IconChevronRight size={12} />}
          >
            {showAll ? "Show fewer" : `Show ${hidden} more`}
          </Button>
        )}
      </div>
    </section>
  );
}

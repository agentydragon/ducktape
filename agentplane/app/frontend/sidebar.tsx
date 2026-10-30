/** The persistent left sidebar: the thread list, the Actions queue, and the footer's page links. */
import { ActionIcon, Tooltip } from "@mantine/core";
import { IconBox, IconHeartbeat, IconListCheck, IconPlus, IconSettings, IconX } from "./icons";
import { type JSX, useEffect, useState } from "react";
import { useLocation, useMatch, useNavigate } from "react-router";

import { ActionsSidebarSection } from "./actions/sidebar";
import { ResizeHandle, usePaneSize } from "../../../util/frontend/resize_handle";
import "./sidebar.css";
import { ThreadList } from "./threads/thread_list";
import { ThreadViewRecorderTools } from "./threads/thread_view_recorder_tools";

// Matches sidebar.css's own phone breakpoint.
const PHONE_QUERY = "(max-width: 560px)";

/** Whether the sidebar is currently rendering as the phone-width full-screen overlay rather than a
 * docked desktop column -- the two need different close-on-navigate behavior below. */
function usePhoneWidth(): boolean {
  const [phone, setPhone] = useState(() => window.matchMedia(PHONE_QUERY).matches);
  useEffect(() => {
    const query = window.matchMedia(PHONE_QUERY);
    const onChange = (event: MediaQueryListEvent): void => setPhone(event.matches);
    query.addEventListener("change", onChange);
    return () => query.removeEventListener("change", onChange);
  }, []);
  return phone;
}

type SidebarProps = {
  settingsOpen: boolean;
  onOpenSettings: () => void;
  /** A phone-width overlay, or a docked column on desktop. */
  open: boolean;
  onClose: () => void;
};

export function Sidebar({ settingsOpen, onOpenSettings, open, onClose }: SidebarProps): JSX.Element {
  const navigate = useNavigate();
  const location = useLocation();
  const pane = usePaneSize("agentplane-sidebar-width", 240, 180, 480);
  const phone = usePhoneWidth();
  const actionsPage = useMatch("/actions");

  useEffect(() => {
    // Escape dismisses the phone-width overlay, the way it would any other full-screen modal.
    // At desktop width there's nothing modal to dismiss -- collapsing the persistent column would
    // just be a surprising side effect of a keypress unrelated to the sidebar.
    if (!open || !phone) return;
    function onKeyDown(event: KeyboardEvent): void {
      if (event.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [open, phone, onClose]);

  /** Every navigation out of the sidebar also closes it, but only at phone width, where it's a
   * full-screen overlay standing in the way of the page it just navigated to. At desktop width
   * this is a no-op: collapsing the persistent column on every click would undo the reader's own
   * choice to keep it open just because they used it. */
  function closeIfPhone(): void {
    if (phone) onClose();
  }

  function goTo(path: string): void {
    closeIfPhone();
    void navigate(path);
  }

  return (
    <nav className={`agentplane-sidebar${open ? " agentplane-sidebar-open" : ""}`} style={{ width: `${pane.size}px` }}>
      <ResizeHandle pane={pane} axis="x" className="agentplane-sidebar-resize-handle" aria-label="Resize sidebar" />
      <div className="agentplane-sidebar-header">
        <div style={{ display: "flex", gap: 2, alignItems: "center" }}>
          {/* The unscoped new-thread composer isn't built yet, so "+" sends the operator to the
              Sandbox list to start one the existing way -- label says exactly that rather than
              promising a composer that isn't there yet. */}
          <Tooltip label="New thread (via Sandboxes)" withArrow>
            <ActionIcon variant="light" aria-label="New thread (via Sandboxes)" onClick={() => goTo("/sandboxes")}>
              <IconPlus size={13} />
            </ActionIcon>
          </Tooltip>
          {/* Only shown by sidebar.css's phone-width, drawer-open state: at desktop width the
              always-visible shell topbar's own toggle already collapses this column. */}
          <ActionIcon
            className="agentplane-sidebar-close"
            variant="subtle"
            aria-label="Close navigation"
            onClick={onClose}
          >
            <IconX size={16} />
          </ActionIcon>
        </div>
      </div>
      <div className="agentplane-sidebar-body">
        <ThreadList detailed={false} onNavigate={closeIfPhone} />
        {/* The Actions page is the queue, so the section would only repeat it there. */}
        {actionsPage === null && <ActionsSidebarSection onNavigate={closeIfPhone} />}
      </div>
      <div className="agentplane-sidebar-footer">
        <div className="agentplane-sidebar-recorder">
          <ThreadViewRecorderTools />
        </div>
        <Tooltip label="Sandboxes" withArrow>
          <ActionIcon
            variant={location.pathname === "/sandboxes" ? "light" : "subtle"}
            aria-label="Sandboxes"
            onClick={() => goTo("/sandboxes")}
          >
            <IconBox size={15} />
          </ActionIcon>
        </Tooltip>
        <Tooltip label="All actions" withArrow>
          <ActionIcon
            variant={location.pathname.startsWith("/actions") ? "light" : "subtle"}
            aria-label="All actions"
            onClick={() => goTo("/actions")}
          >
            <IconListCheck size={15} />
          </ActionIcon>
        </Tooltip>
        <Tooltip label="System status" withArrow>
          <ActionIcon
            variant={location.pathname === "/system" ? "light" : "subtle"}
            aria-label="System status"
            onClick={() => goTo("/system")}
          >
            <IconHeartbeat size={15} />
          </ActionIcon>
        </Tooltip>
        <Tooltip label="Settings" withArrow>
          <ActionIcon
            variant={settingsOpen ? "light" : "subtle"}
            aria-label="Settings"
            onClick={() => {
              closeIfPhone();
              onOpenSettings();
            }}
          >
            <IconSettings size={15} />
          </ActionIcon>
        </Tooltip>
      </div>
    </nav>
  );
}

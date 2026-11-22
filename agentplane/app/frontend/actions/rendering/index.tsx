// React presentation registry for Actions. Each Action can choose a human-facing label, a different
// widget for the collapsed and opened Actions pane, and full-detail arguments and results. The parallel,
// React-free notification registry lives in ../notifications because the service worker imports it.
// The group is the name the Action Service configures a backend under. Unknown identities or
// payloads fall back to the host's generic view.
import { ActionRendering, type ActionRegistry } from "./dispatch";
import { kubernetesAdminPresentationGroup } from "./kubernetes_admin/index";
import { githubPresentationGroup } from "./github/index";
import { homeAssistantPresentationGroup } from "./home_assistant/group";
import { gmailPresentationGroup } from "./gmail";
import { googleCalendarPresentationGroup } from "./google_calendar";
import { grocyPresentationGroup } from "./grocy";
import { tanaPresentationGroup } from "./tana";
import { sandboxPresentationGroup } from "./sandbox";
import { orchestrationPresentationGroup } from "./orchestration";
import { environmentsPresentationGroup, sessionsPresentationGroup } from "./session_delegation";
import { sshPresentationGroup } from "./ssh/index";

// Maps avoid prototype-key lookups for group and Action names.
export const ACTION_RENDERERS: ActionRegistry = new Map([
  kubernetesAdminPresentationGroup,
  githubPresentationGroup,
  gmailPresentationGroup,
  googleCalendarPresentationGroup,
  homeAssistantPresentationGroup,
  grocyPresentationGroup,
  tanaPresentationGroup,
  sandboxPresentationGroup,
  environmentsPresentationGroup,
  sessionsPresentationGroup,
  orchestrationPresentationGroup,
  sshPresentationGroup,
]);

export const appActionRendering: ActionRendering = new ActionRendering(ACTION_RENDERERS);

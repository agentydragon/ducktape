import { offersCardDecisionButtonsEventsList, eventsListLabel, eventsListPane } from "./events_list";
import {
  offersCardDecisionButtonsPodsInNamespace,
  podsInNamespaceCollapsed,
  podsInNamespaceDetails,
  podsInNamespaceLabel,
  podsInNamespacePane,
  podsInNamespaceTitleIsRedundant,
} from "./pods_list_in_namespace";
import { podsDeleteLabel, podsDeletePane, podsExecCollapsed, podsExecLabel, podsExecPane } from "./pods_exec";
import { offersCardDecisionButtonsPodsGet, podsGetLabel, podsGetPreview } from "./pods_get";
import { offersCardDecisionButtonsPodsLog, podsLogLabel, podsLogPreview } from "./pods_log";
import { resourcesApplyCollapsed, resourcesApplyLabel, resourcesApplyManifest } from "./resources_apply";
import { resourcesDeleteLabel, resourcesDeletePane } from "./resources_delete";
import { offersCardDecisionButtonsResourcesGet, resourcesGetLabel, resourcesGetPane } from "./resources_get";
import { offersCardDecisionButtonsResourcesList, resourcesListLabel, resourcesListPane } from "./resources_list";
import type { ActionPresentation } from "../presentation";

export const kubernetesAdminRenderers: ReadonlyMap<string, ActionPresentation> = new Map<string, ActionPresentation>([
  [
    "pods_list_in_namespace",
    {
      label: podsInNamespaceLabel,
      pane: {
        collapsed: podsInNamespaceCollapsed,
        opened: podsInNamespacePane,
        requestTitleIsRedundant: podsInNamespaceTitleIsRedundant,
      },
      details: { arguments: podsInNamespaceDetails },
      offersCardDecisionButtons: offersCardDecisionButtonsPodsInNamespace,
    },
  ],
  [
    "resources_create_or_update",
    {
      label: resourcesApplyLabel,
      pane: { collapsed: resourcesApplyCollapsed, opened: resourcesApplyManifest },
      details: { arguments: resourcesApplyManifest },
    },
  ],
  ["pods_delete", { label: podsDeleteLabel, pane: { opened: podsDeletePane }, details: { arguments: podsDeletePane } }],
  [
    "pods_exec",
    {
      label: podsExecLabel,
      pane: { collapsed: podsExecCollapsed, opened: podsExecPane },
      details: { arguments: podsExecPane },
    },
  ],
  [
    "resources_get",
    {
      label: resourcesGetLabel,
      pane: { opened: resourcesGetPane },
      details: { arguments: resourcesGetPane },
      offersCardDecisionButtons: offersCardDecisionButtonsResourcesGet,
    },
  ],
  [
    "resources_list",
    {
      label: resourcesListLabel,
      pane: { opened: resourcesListPane },
      details: { arguments: resourcesListPane },
      offersCardDecisionButtons: offersCardDecisionButtonsResourcesList,
    },
  ],
  [
    "resources_delete",
    {
      label: resourcesDeleteLabel,
      pane: { opened: resourcesDeletePane },
      details: { arguments: resourcesDeletePane },
    },
  ],
  [
    "pods_get",
    {
      label: podsGetLabel,
      pane: { opened: podsGetPreview },
      details: { arguments: podsGetPreview },
      offersCardDecisionButtons: offersCardDecisionButtonsPodsGet,
    },
  ],
  [
    "pods_log",
    {
      label: podsLogLabel,
      pane: { opened: podsLogPreview },
      details: { arguments: podsLogPreview },
      offersCardDecisionButtons: offersCardDecisionButtonsPodsLog,
    },
  ],
  [
    "events_list",
    {
      label: eventsListLabel,
      pane: { opened: eventsListPane },
      details: { arguments: eventsListPane },
      offersCardDecisionButtons: offersCardDecisionButtonsEventsList,
    },
  ],
]);

import type { ActionPresentation } from "../presentation";
import { podsInNamespacePresentation } from "./pods_list_in_namespace";
import { resourcesApplyPresentation } from "./resources_apply";
import { podsDeletePresentation, podsExecPresentation } from "./pods_exec";
import { resourcesGetPresentation } from "./resources_get";
import { resourcesListPresentation } from "./resources_list";
import { resourcesDeletePresentation } from "./resources_delete";
import { podsGetPresentation } from "./pods_get";
import { podsLogPresentation } from "./pods_log";
import { eventsListPresentation } from "./events_list";

export const kubernetesAdminRenderers: ReadonlyMap<string, ActionPresentation> = new Map([
  ["pods_list_in_namespace", podsInNamespacePresentation],
  ["resources_create_or_update", resourcesApplyPresentation],
  ["pods_delete", podsDeletePresentation],
  ["pods_exec", podsExecPresentation],
  ["resources_get", resourcesGetPresentation],
  ["resources_list", resourcesListPresentation],
  ["resources_delete", resourcesDeletePresentation],
  ["pods_get", podsGetPresentation],
  ["pods_log", podsLogPresentation],
  ["events_list", eventsListPresentation],
]);

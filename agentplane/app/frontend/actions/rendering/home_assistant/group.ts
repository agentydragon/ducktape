import { homeAssistantOrganizationRenderers } from "./organization";
import { homeAssistantOperationsRenderers } from "./operations";
import { homeAssistantHistoryRenderers } from "./history";
import { homeAssistantRenderers } from "../home_assistant";

const homeAssistantGroup = new Map([
  ...homeAssistantRenderers,
  ...homeAssistantHistoryRenderers,
  ...homeAssistantOrganizationRenderers,
  ...homeAssistantOperationsRenderers,
]);

export const homeAssistantPresentationGroup: readonly [string, typeof homeAssistantGroup] = [
  "home_assistant",
  homeAssistantGroup,
];

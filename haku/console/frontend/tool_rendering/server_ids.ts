// The MCP server ids the console renders widgets for. A leaf module (no imports at all) so both
// the React widget registry and the React-free action registry (`actions.ts`, which the service
// worker bundles) can key off the same constants without either pulling in the other's deps.
//
// These match the `id`s in haku-console's server catalog (cluster/k8s/haku/console/config.yaml);
// a tool call's `server_id` is compared against them verbatim.

export const GMAIL_SERVER_ID = "gmail";
export const GOOGLE_CALENDAR_SERVER_ID = "google_calendar";
export const GRANTS_SERVER_ID = "grants";
export const GROCY_SERVER_ID = "grocy-sf";
export const HAKU_ROUTINE_SERVER_ID = "haku_routine";
export const HOSTEXEC_SERVER_ID = "hostexec";
export const KUBECTL_SERVER_ID = "kubectl-passthrough-mcp";
export const TANA_RW_SERVER_ID = "tana-rw";

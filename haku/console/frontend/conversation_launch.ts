import type { LaunchOption } from "./client";

/** The rolling-compatible launch catalog: older API replicas omit it entirely. */
export function conversationLaunchOptions(config: { launch_options?: LaunchOption[] | null }): LaunchOption[] {
  return config.launch_options ?? [];
}

export function launchKey(option: LaunchOption): string {
  return `${option.agent_id}:${option.harness_kind}`;
}

export function initialLaunchKey(options: LaunchOption[]): string | null {
  // A sole authorized pair is unambiguous; multiple pairs require the operator to choose.
  return options.length === 1 ? launchKey(options[0]) : null;
}

export function shouldShowLaunchSelector(options: LaunchOption[]): boolean {
  return options.length > 1;
}

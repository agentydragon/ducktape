import type { ChatLaunchOption } from "./client";

/** The rolling-compatible launch catalog: older API replicas omit it entirely. */
export function conversationLaunchOptions(config: {
  chat_launch_options?: ChatLaunchOption[] | null;
}): ChatLaunchOption[] {
  return config.chat_launch_options ?? [];
}

export function launchKey(option: ChatLaunchOption): string {
  return `${option.agent_id}:${option.runtime}`;
}

export function initialLaunchKey(options: ChatLaunchOption[]): string | null {
  // A sole authorized pair is unambiguous; multiple pairs require the operator to choose.
  return options.length === 1 ? launchKey(options[0]) : null;
}

export function shouldShowLaunchSelector(options: ChatLaunchOption[]): boolean {
  return options.length > 1;
}

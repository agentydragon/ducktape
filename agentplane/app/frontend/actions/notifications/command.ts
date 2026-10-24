/** A command-running Action's notification body: the command, then the settings that change how it runs. */
export function commandNotificationText(command: string, notes: readonly string[]): string {
  return [`$ ${command}`, ...notes].join(" · ");
}

/** Settings that change how a Pod command runs; the opened card and the notification share them. */
export function podsExecNotes(args: { container?: string | undefined }): string[] {
  return args.container === undefined ? [] : [`Container ${args.container}`];
}

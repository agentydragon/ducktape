/** How a log read is cut, as label suffixes; the app's label and the notification title share them. */
export function podsLogOptions(args: {
  container?: string | undefined;
  previous?: boolean | undefined;
  tail?: number | undefined;
}): string[] {
  const { container, previous, tail } = args;
  return [
    ...(container === undefined ? [] : [`container ${container}`]),
    ...(previous === true ? ["previous run"] : []),
    // Only a positive tail is a line count; any other value is shown as given.
    ...(tail === undefined ? [] : [tail > 0 ? `last ${tail} ${tail === 1 ? "line" : "lines"}` : `tail ${tail}`]),
  ];
}

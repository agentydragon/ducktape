// React-free, so the service worker's notification formatters show argv the way the app does.

function shellQuote(argument: string): string {
  return /^[A-Za-z0-9_./:=,@+-]+$/.test(argument) ? argument : `'${argument.replaceAll("'", "'\\''")}'`;
}

/** A shell-safe display of argv that preserves each argument boundary without implying shell execution. */
export function argvText(argv: readonly string[]): string {
  return argv.map(shellQuote).join(" ");
}

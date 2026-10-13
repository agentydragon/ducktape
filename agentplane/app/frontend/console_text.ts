/** The text a console view shows for the raw bytes a serial console emits. */
// Terminal control sequences (CSI and OSC) are dropped: this is a text view, not a terminal.
// eslint-disable-next-line no-control-regex
const CONTROL_SEQUENCES = /\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]|\r/g;
const MAX_CHARS = 200_000;

export function consoleText(previous: string, output: string): string {
  return (previous + output.replace(CONTROL_SEQUENCES, "")).slice(-MAX_CHARS);
}

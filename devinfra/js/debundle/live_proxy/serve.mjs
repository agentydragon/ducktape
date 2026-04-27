import { runServeCli } from "./proxy.mjs";

try {
  const exitCode = await runServeCli(process.argv.slice(2));
  process.exitCode = exitCode;
} catch (error) {
  process.stderr.write(`${error?.stack ?? error}\n`);
  process.exitCode = 1;
}

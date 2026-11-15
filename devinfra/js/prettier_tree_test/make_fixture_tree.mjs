import { promises as fs } from "node:fs";
import * as path from "node:path";

const [outputArg] = process.argv.slice(2);
if (!outputArg) throw new Error("usage: make_fixture_tree.mjs <output-tree>");

const outputDir = path.resolve(process.env.JS_BINARY__EXECROOT ?? process.cwd(), outputArg);
await fs.mkdir(path.join(outputDir, "app", "nested"), { recursive: true });
await fs.writeFile(path.join(outputDir, "outside.js"), "const outside={answer:42}\n");
await fs.writeFile(path.join(outputDir, "app", "main.js"), "const message='hello';console.log(message)\n");
await fs.writeFile(path.join(outputDir, "app", "nested", "values.js"), "export const values=[1,2,3]\n");
await fs.writeFile(path.join(outputDir, "app", "nested", "payload.bin"), Buffer.from([0, 255, 128, 10, 13, 1]));
await fs.writeFile(path.join(outputDir, "app", "nested", "note.txt"), Buffer.from([0x68, 0x69, 0x00, 0xfe]));

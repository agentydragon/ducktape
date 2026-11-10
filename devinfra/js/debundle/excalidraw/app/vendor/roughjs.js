// Vendor entry: a second entry point reaching roughjs (but not the editor)
// makes esbuild split roughjs into its own shared chunk.
export { default } from "roughjs/bin/rough";

// Vendor entry: a second entry point reaching clsx (but not the editor)
// makes esbuild split clsx into its own shared chunk.
export { default } from "clsx";

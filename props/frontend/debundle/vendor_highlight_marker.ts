// Smoke-bundle marker for highlight.js. Forces esbuild's chunk-graph
// algorithm to put highlight.js into its own shared chunk (since this
// marker entry shares only highlight.js with the main app entry).

import hljs from "highlight.js";

export const __highlightAlive = hljs;

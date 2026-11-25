// Common module resolution and Tailwind settings for the app and its browser tests.
import tailwindcss from "esbuild-plugin-tailwindcss";
import { dirname, resolve } from "path";
import { fileURLToPath } from "url";

const __dirname = dirname(fileURLToPath(import.meta.url));

export default {
  jsx: "automatic",
  plugins: [tailwindcss()],
  alias: {
    $lib: resolve(__dirname, "src/lib"),
    $components: resolve(__dirname, "src/components"),
  },
  loader: { ".svg": "text" },
  nodePaths: [resolve(__dirname, "node_modules")],
  conditions: ["browser", "module", "import"],
};

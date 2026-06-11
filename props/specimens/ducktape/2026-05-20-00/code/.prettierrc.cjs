// Prettier configuration
// https://prettier.io/docs/en/options.html
//
// Svelte plugin: Loaded via require() so both Nix (NODE_PATH wrapper) and
// Bazel (runfiles) can resolve it. String-based plugin names don't work
// because prettier can't resolve them without node_modules on the search path.

const config = {
  printWidth: 120,
  tabWidth: 2,
  useTabs: false,
  semi: true,
  singleQuote: false,
  trailingComma: "es5",
  bracketSpacing: true,
  plugins: [require("prettier-plugin-svelte")],
  overrides: [
    {
      files: "*.svelte",
      options: {
        parser: "svelte",
      },
    },
  ],
};

module.exports = config;

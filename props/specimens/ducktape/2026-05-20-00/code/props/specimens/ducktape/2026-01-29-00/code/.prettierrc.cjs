// Prettier configuration
// https://prettier.io/docs/en/options.html
//
// Svelte plugin: Loaded via require() so Bazel can resolve it from runfiles.
// String-based plugin names fail in CI because prettier finds this config in
// the source tree but can't resolve plugins without node_modules.
// See aspect-build/rules_lint#176 and PR #417.

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

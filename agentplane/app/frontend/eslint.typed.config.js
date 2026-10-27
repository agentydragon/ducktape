import typedConfig from "../../../devinfra/js/eslint.typed.config.js";

export default [
  ...typedConfig,
  {
    files: ["service_worker/sw.test.ts"],
    languageOptions: {
      parserOptions: {
        projectService: false,
        project: "service_worker/tsconfig.test.json",
      },
    },
  },
];

import typedConfig from "../../../devinfra/js/eslint.typed.config.js";

export default [
  ...typedConfig,
  {
    files: ["actions/push/service_worker/sw.test.ts"],
    languageOptions: {
      parserOptions: {
        projectService: false,
        project: "actions/push/service_worker/tsconfig.test.json",
      },
    },
  },
];

import { dateOptions } from "./openapi-config-helper.mjs";

export default {
  plugins: [{ name: "zod", dates: dateOptions }],
};

// This application imports only the public entry. Zod's shipped schemas and
// parse helpers invoke the locale through their original static imports.
import * as publicZod from "zod/v3";

const keys = Reflect.ownKeys(publicZod);
const namespace = {
  keys,
  members: keys.map((key) => {
    const { value, enumerable, writable, configurable } = Object.getOwnPropertyDescriptor(publicZod, key);
    return { type: typeof value, enumerable, writable, configurable };
  }),
  nullPrototype: Object.getPrototypeOf(publicZod) === null,
  extensible: Object.isExtensible(publicZod),
  aliased: publicZod.default === publicZod.z && publicZod.z.string === publicZod.string,
  forwardedKeys: Object.keys(publicZod.z),
  forwardedErrorMap: publicZod.z.defaultErrorMap === publicZod.defaultErrorMap,
};

const schema = publicZod.object({ label: publicZod.string().min(3) });
function issues(input) {
  const result = schema.safeParse(input);
  if (result.success) throw new Error("expected invalid input");
  return result.error.issues.map(({ code, path, message }) => ({ code, path, message }));
}

const initialMap = publicZod.getErrorMap() === publicZod.defaultErrorMap;
const missing = issues({});
const wrongType = issues({ label: 42 });
const short = issues({ label: "x" });
const option = publicZod.enum(["short", "long"]).safeParse("other");
if (option.success) throw new Error("expected invalid enum value");
const [invalidEnum] = option.error.issues.map(({ message }) => message);

let overridden;
let installedMap;
const custom = (_issue, context) => ({ message: `form: ${context.defaultError}` });
publicZod.setErrorMap(custom);
try {
  installedMap = publicZod.getErrorMap() === custom;
  overridden = issues({});
} finally {
  publicZod.setErrorMap(publicZod.defaultErrorMap);
}
const restored = issues({});
const valid = schema.parse({ label: "long" });

export const observation = {
  namespace,
  behavior: { initialMap, missing, wrongType, short, invalidEnum, installedMap, overridden, restored, valid },
};

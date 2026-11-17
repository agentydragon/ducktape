let factory;
export function retain(value) {
  if (typeof value === "function") factory = value;
  else
    Object.defineProperty(value, "missing", {
      get() {
        return factory()();
      },
    });
}

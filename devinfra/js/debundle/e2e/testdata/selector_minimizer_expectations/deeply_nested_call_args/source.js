const selectedView = wrapOuter(
  decorate(
    buildInner({
      mode: "uniqueDiscriminatorMode",
      payload: computePayload(Date.now()),
    }),
    { theme: "light" }
  )
);

const firstSiblingView = wrapOuter(
  decorate(
    buildInner({
      mode: "compact",
      payload: computePayload(Date.now()),
    }),
    { theme: "light" }
  )
);

const secondSiblingView = wrapOuter(
  decorate(
    buildInner({
      mode: "wide",
      payload: computePayload(Math.random()),
    }),
    { theme: "dark" }
  )
);

function wrapOuter(value) {
  return value;
}

function decorate(value, options) {
  return { value, options };
}

function buildInner(config) {
  return config;
}

function computePayload(value) {
  return value;
}

export { selectedView };

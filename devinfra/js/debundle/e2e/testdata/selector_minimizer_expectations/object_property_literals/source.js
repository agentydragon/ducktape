const selectedConfig = buildWidget({
  kind: "primary",
  generatedPayload: makePayload(Date.now()),
  mode: "compact",
  volatileStyle: computeStyle(Math.random()),
  onClick: makeCallback("selected"),
});

const sameKindConfig = buildWidget({
  kind: "primary",
  mode: "wide",
});

const sameModeConfig = buildWidget({
  kind: "secondary",
  mode: "compact",
});

function buildWidget(config) {
  return config;
}

function makePayload(value) {
  return value;
}

function computeStyle(value) {
  return value;
}

function makeCallback(value) {
  return value;
}

export { selectedConfig };

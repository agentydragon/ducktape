const selectedLimit = 15,
  buildNoise = makeNoise(Date.now()),
  selectedThreshold = 0.3,
  selectedLabel = makeLabel("selected");

const unrelatedLimit = 15,
  unrelatedThreshold = 0.7;

function makeNoise(value) {
  return value;
}

function makeLabel(value) {
  return value;
}

export { selectedLimit, selectedThreshold };

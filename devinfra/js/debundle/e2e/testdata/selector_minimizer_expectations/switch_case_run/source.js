function selectedRouter(action) {
  switch (action.type) {
    case "init":
      return setup(action);
    case "load":
      return load(action);
    case "unique-target":
      return handleTarget(action);
    case "save":
      return persist(action);
    default:
      return fallback(action);
  }
}

function sameShapeDifferentCases(action) {
  switch (action.type) {
    case "init":
      return setup(action);
    case "load":
      return load(action);
    case "remove":
      return remove(action);
    case "save":
      return persist(action);
    default:
      return fallback(action);
  }
}

function anotherSibling(action) {
  switch (action.type) {
    case "open":
      return open(action);
    case "close":
      return close(action);
    default:
      return fallback(action);
  }
}

function setup(a) {
  return a;
}

function load(a) {
  return a;
}

function handleTarget(a) {
  return a;
}

function persist(a) {
  return a;
}

function fallback(a) {
  return a;
}

function remove(a) {
  return a;
}

function open(a) {
  return a;
}

function close(a) {
  return a;
}

export { selectedRouter };

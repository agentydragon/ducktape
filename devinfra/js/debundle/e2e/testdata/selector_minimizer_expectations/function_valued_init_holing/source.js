const selectedHandler = registerHandler(function (event) {
  logEvent(event);
  dispatch("uniqueDiscriminatorAction");
  cleanup(event);
});

const firstOtherHandler = registerHandler(function (event) {
  logEvent(event);
  dispatch("alphaAction");
  cleanup(event);
});

const secondOtherHandler = registerHandler(function (event) {
  logEvent(event);
  dispatch("betaAction");
  cleanup(event);
});

function registerHandler(fn) {
  return fn;
}
function logEvent(e) {}
function dispatch(action) {}
function cleanup(e) {}

export { selectedHandler };

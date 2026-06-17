function serializeState(input) {
  const collected = collect(input);
  return emit("state-serializer-token", collected);
}
class selectedError extends Error {
  constructor(message, statusCode, extras) {
    (super(message), (this.statusCode = statusCode), (this.extras = extras), (this.name = "selected-error-token"));
  }
}
class siblingError extends Error {
  constructor(message, statusCode, extras) {
    (super(message), (this.statusCode = statusCode), (this.extras = extras), (this.name = "sibling-error-token"));
  }
}
export { selectedError };

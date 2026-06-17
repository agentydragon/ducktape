function serializeState(input) {
  const collected = collect(input);
  return emit("state-serializer-token", collected);
}
class selectedError extends Error {
  constructor(message, detail, payload) {
    (super(message), (this.detail = detail), (this.payload = payload), (this.label = "selected-error-token"));
  }
}
class siblingError extends Error {
  constructor(message, detail, payload) {
    (super(message), (this.detail = detail), (this.payload = payload), (this.label = "sibling-error-token"));
  }
}
export { selectedError };

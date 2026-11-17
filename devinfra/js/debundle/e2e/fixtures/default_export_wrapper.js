function unwrapDefault(value) {
  return value && value.__esModule && Object.prototype.hasOwnProperty.call(value, "default") ? value.default : value;
}

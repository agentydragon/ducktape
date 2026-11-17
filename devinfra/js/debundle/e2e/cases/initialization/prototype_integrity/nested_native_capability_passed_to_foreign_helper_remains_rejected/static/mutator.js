export function install(record) {
  record.native.defineProperty(record.target, "weight", { configurable: true, get: record.callback });
}

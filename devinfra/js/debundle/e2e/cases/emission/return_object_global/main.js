function factory() {
  return { logger: console };
}
console.log(factory().logger === console);
export { factory };

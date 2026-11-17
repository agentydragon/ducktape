// Entry must import every emitted owner rather than relying on the mediator.
const dep_value = "alpha";
const cross_value = dep_value + "-beta";
function lazy_reader() {
  return cross_value;
}
function mediator_helper() {
  return dep_value + "-via-mediator-" + lazy_reader();
}
const mediator_init = mediator_helper();
console.log(mediator_init);
export { mediator_init };

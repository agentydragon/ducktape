const target = {
  set x(value) {
    value();
  },
};
function exposeArgument() {
  consume(arguments);
  target.x = arguments[0];
}
const dynamic = (exposeArgument(readLater), 1);
var later = dynamic;
function readLater() {
  return later;
}

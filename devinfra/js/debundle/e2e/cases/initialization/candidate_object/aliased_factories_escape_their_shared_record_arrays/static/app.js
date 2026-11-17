const carrier = { first: [11], last: [29] };
function makeFirst() {
  return carrier;
}
function makeSecond() {
  return carrier;
}
const receiver = true ? makeFirst : makeSecond;
const host = {
  consume(factory) {
    const value = factory();
    console.log(value.first[0], value.last[0]);
  },
};
host.consume(receiver);
host.consume(receiver);

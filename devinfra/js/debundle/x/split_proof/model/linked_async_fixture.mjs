let trace = "";

async function completed(value) {
  trace += "s";
  return value;
}

const payload = {};

async function abrupt() {
  trace += "x";
  throw payload;
}

const first = completed(7);
const afterFirst = trace;
const second = completed(8);
const afterSecond = trace;
const rejected = abrupt();
const afterRejection = trace;
trace += "e";
const afterCaller = trace;

export { first, second, rejected, payload, afterFirst, afterSecond, afterRejection, afterCaller };

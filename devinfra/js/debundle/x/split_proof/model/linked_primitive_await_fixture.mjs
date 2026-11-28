let trace = "";
let marker = 0;
const payload = {};

async function rejectAfter(mark) {
  const reason = payload;
  trace += mark;
  const probe = {
    get value() {
      trace += "g";
      return 0;
    },
  };
  await probe.value;
  trace += "A";
  throw reason;
}

async function fulfillAfter(mark) {
  const offset = 3;
  trace += mark;
  await 0;
  trace += "B";
  return marker + offset;
}

const first = rejectAfter("a");
const atReturn = trace;
const second = fulfillAfter("b");
marker = 20;
trace += "|";
const duringTurn = trace;

export { first, second, payload, trace, atReturn, duringTurn };

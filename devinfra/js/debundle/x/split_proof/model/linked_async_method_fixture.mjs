export let trace = "";
let operand = "at-call";
let offset = 0;
export const payload = {};
class Worker {
  async run(tag, fails) {
    trace += tag;
    await operand;
    trace += this.label;
    if (fails) throw payload;
    return this.value + offset;
  }

  async saveOperand() {
    return await operand;
  }
}

const firstReceiver = new Worker();
const secondReceiver = new Worker();
firstReceiver.value = 1;
firstReceiver.label = "old";
secondReceiver.label = "old";
export const first = firstReceiver.run("a", false);
export const second = secondReceiver.run("b", true);
export const last = secondReceiver.saveOperand();
firstReceiver.label = "A";
secondReceiver.label = "B";
firstReceiver.value = 22;
offset = 20;
operand = "after-call";

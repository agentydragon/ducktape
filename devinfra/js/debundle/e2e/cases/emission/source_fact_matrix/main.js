const object0 = {};
const object1 = {};
const object2 = {};
const object3 = {};
let receiver = object0;
receiver = object1;
receiver = object2;
receiver = object3;
receiver.p0 = () => 0;
receiver.p1 = () => 1;
receiver.p2 = () => 2;
receiver.p3 = () => 3;
function observe() {
  return [receiver.p0, receiver.p1, receiver.p2, receiver.p3];
}
console.log(receiver.p0(), receiver.p3());

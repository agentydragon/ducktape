let later = 0;
let observed = -1;
Array.prototype.forEach = function () {
  observed = later;
};
const result = [{ id: "a" }].forEach(() => 0);
later = 42;
console.log(observed);

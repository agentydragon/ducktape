let later = 0;
let seen = 0;
const entries = [{ id: "a" }, { id: "b" }];
entries.some((entry) => {
  seen += 1;
  return entry.id === "b";
});
const copy = seen;
later = 42;
console.log(copy, seen, later);

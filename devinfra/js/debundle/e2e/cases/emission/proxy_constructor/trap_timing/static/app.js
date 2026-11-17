const a = new Proxy(
  { value: 1 },
  {
    get(target, key) {
      console.log("trap", key);
      return target[key];
    },
  }
);
const b = new Proxy(
  { value: 2 },
  {
    get(target, key) {
      console.log("trap", key);
      return target[key];
    },
  }
);
const c = new Proxy(
  { value: 3 },
  {
    get(target, key) {
      console.log("trap", key);
      return target[key];
    },
  }
);
console.log(a.value, b.value, c.value);
export { a, b, c };

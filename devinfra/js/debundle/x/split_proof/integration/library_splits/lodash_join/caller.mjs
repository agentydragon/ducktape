// This module is staged unchanged beside each public join.js entry.
import join from "./join.js";

export { join };
export let writes = 0;

export function tryJoin(array, separator) {
  try {
    return { ending: "returned", value: join(array, separator) };
  } catch (value) {
    return { ending: "threw", value };
  }
}

export function pair(events, thrown) {
  const first = {
    toString() {
      events.push(this === first ? "first:this" : "first:wrong-this");
      writes += 1;
      return "one";
    },
  };
  const second = {
    toString() {
      events.push(this === second ? "second:this" : "second:wrong-this");
      writes += 10;
      throw thrown;
    },
  };
  const array = {
    get length() {
      events.push(this === array ? "length:this" : "length:wrong-this");
      return 2;
    },
    get 0() {
      events.push(this === array ? "first-slot:this" : "first-slot:wrong-this");
      return first;
    },
    get 1() {
      events.push(this === array ? "second-slot:this" : "second-slot:wrong-this");
      return second;
    },
  };
  return array;
}

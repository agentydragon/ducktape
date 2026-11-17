import { factory } from "./peer.js";
const descriptors = Object.getOwnPropertyDescriptors({
  nested: {
    get value() {
      return later;
    },
  },
});
const wrapped = factory(descriptors);
const later = 42;

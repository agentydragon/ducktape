import { prepare } from "./peer.js";
let later = 0;
const ids = { tool: "tool-id" };
const entries = [{ id: ids.tool, name: "tool", run: () => later }];
const lookup = {};
const latent = {
  get value() {
    return later;
  },
};
prepare();
entries.forEach((entry) => {
  lookup[entry.id] = entry.name;
});
later = 42;
console.log(lookup["tool-id"], entries[0].run());

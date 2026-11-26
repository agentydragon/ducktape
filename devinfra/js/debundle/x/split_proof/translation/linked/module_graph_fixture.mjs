// The original executable units and standalone public export retain their source ordinals.
export function moduleGraphFixture(count = 16) {
  const names = Array.from({ length: count }, (_, index) => `reader${index}`);
  const expected = Object.fromEntries(names.map((name, index) => [name, `value${index}`]));
  const values = names.map((name, index) => `const data${index} = ${JSON.stringify(expected[name])};`);
  const readers = names.map((name, index) => `const ${name} = data${index};`);
  const source = [...values, ...readers, `export { ${names.join(", ")} };`].join("\n");
  const entry = "entry.mjs";
  const modules = {};
  for (const [group, offset] of [
    ["provider", 0],
    ["consumer", count],
  ]) {
    for (let index = 0; index < count; index++) {
      const module = `${group}${Math.floor(index / 8)}.mjs`;
      (modules[module] ??= []).push(`#${offset + index}`);
    }
  }
  const order = Object.keys(modules);
  modules[entry] = [`#${2 * count}`];
  return { names, expected, source, entry, order, spec: { entry, modules, order, sequential: true } };
}

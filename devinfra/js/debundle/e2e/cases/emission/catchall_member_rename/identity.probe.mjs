export default async (entry, entryUrl, sourceUrl) => {
  const residual = await import(new URL("./modules/residual/unhandled.js", entryUrl));
  const original = await import(sourceUrl);
  console.log(entry.a === entry.FirstFn, entry.a === residual.FirstFn, entry.a !== entry.b);
  console.log(
    JSON.stringify({ name: original.a.name, descriptor: Object.getOwnPropertyDescriptor(original.a, "name") })
  );
  console.log(JSON.stringify({ name: entry.a.name, descriptor: Object.getOwnPropertyDescriptor(entry.a, "name") }));
};

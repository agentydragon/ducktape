export default async (entry, entryUrl) => {
  const residual = await import(new URL("./modules/residual/unhandled.js", entryUrl));
  console.log(entry.a === entry.FirstFn, entry.a === residual.FirstFn, entry.a !== entry.b);
};

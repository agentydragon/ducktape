export default function probe(entry) {
  globalThis.__updateState();
  console.log(entry.state);
}

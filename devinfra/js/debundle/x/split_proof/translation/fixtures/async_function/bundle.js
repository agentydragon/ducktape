async function load() {
  await Promise.resolve();
  return 1;
}
load().then(console.log);

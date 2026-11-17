class C {
  async run() {
    return await Promise.resolve("ok");
  }
}
const c = new C();
c.run().then((v) => console.log(v));
export { C };

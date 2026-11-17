async function f() { return 1; }
class C {
  static x = (async () => await f())();
  static y = await f();
}
console.log(C.x, C.y);
export { C };

const registry = {
  register(x) {
    (this.items ||= []).push(x);
  },
  addPlugin(x) {
    (this.plugins ||= []).push(x);
  },
};
class FooService {}
class BarPlugin {}
registry.register(FooService);
registry.addPlugin(BarPlugin);
console.log(new FooService().constructor.name, new BarPlugin().constructor.name);
export { registry, FooService, BarPlugin };

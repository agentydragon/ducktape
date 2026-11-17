const host = {
  define(name, ctor, mixin) {
    (this.m ||= {})[name] = [ctor, mixin];
  },
};
class Widget {}
class Mixin {}
host.define("widget", Widget, Mixin);
console.log(new Widget().constructor.name, new Mixin().constructor.name);
export { host, Widget, Mixin };

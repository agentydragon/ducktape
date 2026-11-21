// Scoping corner cases. Every statement is valid module code. Compared against eslint-scope.
const a = 1,
  b = 2,
  c = 3,
  d = 4,
  e = 5,
  g = () => 6;
function f1(x = g(), y = a) {
  var a2 = 1;
  return x + y + b + a2;
}
function f2(p = q, q = 1) {
  return p;
}
function f3(x = outerVar) {
  var outerVar = 1;
  return x;
}
var outerVar = 7;
function f4() {
  try {
    throw 1;
  } catch (a) {
    return a;
  }
  return a;
}
function f5() {
  for (const k of [1]) {
    setTimeout(() => k + b);
  }
}
const o = {
  [c]: 1,
  d,
  m() {
    return e;
  },
  get x() {
    return a;
  },
  ...{ b },
};
class K extends (g(), Object) {
  static s = d;
  i = e;
  [c] = 1;
  #p = a;
  static {
    b;
  }
  m() {
    return K;
  }
}
label: for (var i = 0; i < 1; i++) {
  break label;
}
const { aa = g(), bb: [cc = a] = [], ...rest } = {};
let [u = b, v = c] = [];
(function named() {
  return named;
})();
const arrow = async (z = e) => await z;
if (a) {
  function inner() {
    return d;
  }
} else {
  var hoisted = 1;
}
switch (a) {
  case 1:
    let sw = 1;
    break;
  default:
    sw;
}
function f6(a, b = () => a) {
  {
    let a = 2;
  }
  return b;
}
const f7 = function (arguments_, ...more) {
  return [arguments_, more, e];
};
function f8() {
  return typeof undeclaredThing;
}
function f9(o2) {
  const { [c]: dyn = d } = o2;
  return dyn;
}
function f10() {
  let b = 1;
  {
    let b = 2;
  }
  return b + c;
}
function f11() {
  try {
    return a;
  } catch (a) {
    return 1;
  }
}
export { a as ax, f1 };

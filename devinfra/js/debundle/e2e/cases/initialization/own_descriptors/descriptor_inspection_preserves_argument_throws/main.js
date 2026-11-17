let observed;
try {
  Object.getOwnPropertyDescriptors({
    field: (() => {
      throw new Error("argument");
    })(),
    get value() {
      return later;
    },
  });
} catch (error) {
  observed = error.message;
}
const later = 42;
console.log(observed, later);

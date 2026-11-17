let later = 0;
let observed = -1;
const holder = { inner: {} };
holder.inner.run = () => {
  observed = later;
};
const result = holder.inner.run();
later = 42;
console.log(observed);

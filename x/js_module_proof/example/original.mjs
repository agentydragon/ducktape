let count = 0;

function increment() {
  count += 1;
}

function current() {
  return count;
}

export { increment, current };

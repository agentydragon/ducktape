let later = 0;
const entries = [() => later];
const result = entries.map((callback) => callback);
later = 42;
console.log(result[0]());

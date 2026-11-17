let later = 0;
const entries = [];
const result = entries.map(() => later);
later = 42;
console.log(result.length);

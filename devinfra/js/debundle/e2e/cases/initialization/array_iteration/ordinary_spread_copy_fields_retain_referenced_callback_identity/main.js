let later = 0;
const entries = [{ run: () => later }];
const result = entries.map((entry) => ({ ...entry }));
const observed = result[0].run();
later = 42;
console.log(observed);

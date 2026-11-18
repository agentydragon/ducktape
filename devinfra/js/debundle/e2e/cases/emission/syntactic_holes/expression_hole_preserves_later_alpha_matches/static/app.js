const limit = 4;
const alpha = 1,
  beta = 2,
  gamma = 3;
const total = Math.max(Math.min(alpha, beta, gamma), limit);
console.log(total);
export { total };

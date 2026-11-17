// No analyzed consumer: public aliases must survive without cross-chunk demand.
const a = 1;
export { a as foo, a as "x-y" };

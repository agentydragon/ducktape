function applyDecorators(target, decorators) {
  for (const decorate of decorators) decorate(target);
}
function decorate(target, decorators) {
  return (applyDecorators(target, decorators), target);
}
const tagged = decorate({ tags: [] }, [(t) => t.tags.push("a")]);
console.log(tagged.tags.join(","));
export { decorate };

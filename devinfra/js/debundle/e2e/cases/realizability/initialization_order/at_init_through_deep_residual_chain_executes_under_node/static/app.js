let T = "ready";
function startTracking() {
  return T;
}
function bootstrap() {
  return startTracking();
}
const init = bootstrap();
console.log(init);
export { T, bootstrap, startTracking, init };

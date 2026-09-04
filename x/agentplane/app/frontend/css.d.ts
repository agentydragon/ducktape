// A stylesheet import is a side effect for the bundler, not a module with a shape: esbuild pulls
// the CSS into the bundle's stylesheet and tsc needs only to know the specifier resolves. Must stay
// a global script (no top-level import/export) so the declaration registers globally.
declare module "*.css";

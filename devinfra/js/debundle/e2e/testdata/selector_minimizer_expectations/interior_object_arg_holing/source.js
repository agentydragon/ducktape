// The discriminating anchor (`mode: "uniqueDiscriminatorMode"`) is one property
// of a nested object literal passed as a call argument; every other property is
// shared across the siblings. Today the minimizer keeps the whole object
// verbatim; it should hole the non-anchor properties to ANYTHING (interior
// holing of a nested object within a kept statement).
function selectedMover(ctx) {
  ctx.engine.run([
    {
      source: ctx.node,
      target: ctx.dest,
      mode: "uniqueDiscriminatorMode",
      silent: true,
      retain: false,
    },
  ]);
}

function firstSiblingMover(ctx) {
  ctx.engine.run([{ source: ctx.node, target: ctx.dest, mode: "alphaMode", silent: true, retain: false }]);
}

function secondSiblingMover(ctx) {
  ctx.engine.run([{ source: ctx.node, target: ctx.dest, mode: "bravoMode", silent: true, retain: false }]);
}

export { selectedMover };

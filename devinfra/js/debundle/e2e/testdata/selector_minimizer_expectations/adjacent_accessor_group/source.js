// Four tiny accessor functions declared right after one another, each a
// one-liner that reads a different property off the same resolved context. Each
// is individually discriminable (by its trailing property), but they are a DRY
// cluster: emitting four near-identical standalone source_match selectors is
// wasteful. They should collapse into ONE binding_group whose source_match is
// the consecutive run of the four functions, with `exports` mapping each.
function selectedAlphaAccessor() {
  return resolveContext().services.alpha;
}
function selectedBetaAccessor() {
  return resolveContext().services.beta;
}
function selectedGammaAccessor() {
  return resolveContext().services.gamma;
}
function selectedDeltaAccessor() {
  return resolveContext().coreServices;
}

function unrelatedHelper(value) {
  return value * 2;
}

export { selectedAlphaAccessor, selectedBetaAccessor, selectedGammaAccessor, selectedDeltaAccessor };

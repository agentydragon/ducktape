// All three classes share the same constructor / render / dispose shape, so no
// unique method name (constructor, dispose) discriminates: the only feature that
// singles out selectedWidget is the pair `format` + "stable" inside render
// (sameRenderDifferentLiteral has `format`+"volatile"; sameLiteralDifferentMethod
// has `paint`+"stable"). This forces the minimizer to anchor on the member body,
// not on a structurally-unique member name.
class selectedWidget {
  constructor(seed) {
    this.seed = normalizeSeed(seed);
  }

  render(view) {
    const transient = computeTransient(this.seed);
    return view.format("stable", transient);
  }

  dispose() {
    releaseWidget(this.seed);
  }
}

class sameRenderDifferentLiteral {
  constructor(seed) {
    this.seed = normalizeSeed(seed);
  }

  render(view) {
    const transient = computeTransient(this.seed);
    return view.format("volatile", transient);
  }

  dispose() {
    releaseWidget(this.seed);
  }
}

class sameLiteralDifferentMethod {
  constructor(seed) {
    this.seed = normalizeSeed(seed);
  }

  render(view) {
    const transient = computeTransient(this.seed);
    return view.paint("stable", transient);
  }

  dispose() {
    releaseWidget(this.seed);
  }
}

function normalizeSeed(seed) {
  return seed;
}

function computeTransient(seed) {
  return seed;
}

function releaseWidget(seed) {
  return seed;
}

export { selectedWidget };

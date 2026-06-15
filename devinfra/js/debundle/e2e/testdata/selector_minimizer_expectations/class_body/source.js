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
  render(view) {
    return view.format("volatile", this.seed);
  }
}

class sameLiteralDifferentMethod {
  render(view) {
    return view.paint("stable", this.seed);
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

// Four tiny sibling class declarations right after one another, each a
// data-holder with a single discriminating `kind` field and an otherwise shared
// shape. Each is individually discriminable (by its `kind` value), but they are
// a DRY cluster: emitting four near-identical standalone source_match selectors
// is wasteful. The general co-occurrence grouping trigger (not function-only)
// collapses them into ONE binding_group whose source_match is the consecutive
// run of the four `class …Card { kind = "…"; ANYTHING; }` declarations.
class selectedAlphaCard {
  kind = "uniqueAlphaCard";
  render() {
    return this.title;
  }
}
class selectedBetaCard {
  kind = "uniqueBetaCard";
  render() {
    return this.title;
  }
}
class selectedGammaCard {
  kind = "uniqueGammaCard";
  render() {
    return this.title;
  }
}
class selectedDeltaCard {
  kind = "uniqueDeltaCard";
  render() {
    return this.title;
  }
}

class unrelatedWidget {
  draw() {
    return null;
  }
}

export { selectedAlphaCard, selectedBetaCard, selectedGammaCard, selectedDeltaCard };

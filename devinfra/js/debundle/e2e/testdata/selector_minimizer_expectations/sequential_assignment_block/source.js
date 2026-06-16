function selectedReducer(state, data) {
  state.alpha = data.alpha;
  state.bravo = data.bravo;
  state.charlie = data.charlie;
  state.delta = "uniqueDiscriminatorValue";
  state.echo = data.echo;
  state.foxtrot = data.foxtrot;
  return state;
}

function firstSiblingReducer(state, data) {
  state.alpha = data.alpha;
  state.bravo = data.bravo;
  state.charlie = data.charlie;
  state.delta = "alpha";
  state.echo = data.echo;
  return state;
}

function secondSiblingReducer(state, data) {
  state.alpha = data.alpha;
  state.bravo = data.bravo;
  state.echo = data.echo;
  state.foxtrot = data.foxtrot;
  return state;
}

export { selectedReducer };

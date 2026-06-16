function selectedComponent(props) {
  const { alpha, bravo, charlie, delta, echo, foxtrot, golf, hotel, india, uniqueDiscriminatorProp } = props;
  const handle = makeHandle(alpha, bravo);
  return render(handle, uniqueDiscriminatorProp);
}

function firstSiblingComponent(props) {
  const { alpha, bravo, charlie, delta, echo, foxtrot, golf, hotel, india } = props;
  const handle = makeHandle(alpha, bravo);
  return render(handle, charlie);
}

function secondSiblingComponent(props) {
  const { alpha, bravo, charlie, delta, echo, foxtrot, golf, hotel } = props;
  const handle = makeHandle(alpha, delta);
  return render(handle, echo);
}

function makeHandle(a, b) {
  return [a, b];
}

function render(handle, value) {
  return { handle, value };
}

export { selectedComponent };

class selectedRunner {
  constructor(node, viewDef, recreate, remove) {
    ((this.node = node),
      (this.viewDef = viewDef),
      (this.recreate = recreate),
      (this.remove = remove),
      track(this),
      (this.boxedStatus = box("stopped", { name: "uniqueDiscriminatorStatus" })),
      (this.viewDefAccessor = viewDef ? new Accessor(viewDef) : void 0),
      this.computeReachableIds(),
      this.initialize());
  }
  boxedStatus;
  newHitCount = 0;
  removedHitCount = 0;
  get status() {
    return this.boxedStatus.get();
  }
  computeReachableIds() {
    const ids = collectIds(this.node);
    this.reachableIds = new Set(ids);
    return this.reachableIds;
  }
  initialize() {
    this.subscription = subscribe(this.node, (change) => {
      this.applyChange(change);
    });
  }
  applyChange(change) {
    if (change.added) this.newHitCount += change.added.length;
    if (change.removed) this.removedHitCount += change.removed.length;
    this.boxedStatus.set("running");
  }
  dispose() {
    this.subscription?.unsubscribe();
    this.boxedStatus.set("stopped");
  }
}

export { selectedRunner };

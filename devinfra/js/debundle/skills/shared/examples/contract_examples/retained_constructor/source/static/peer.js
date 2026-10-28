export class Box {
  constructor(callback, metadata) {
    this.callback = callback;
    this.metadata = metadata;
  }
  invoke() {
    return this.callback();
  }
}

import * as accessors from "./accessors.js";
class CardsView extends accessors.CardsBase {}
class TreeView extends accessors.TreeBase {}
console.log(new CardsView().kind(), new TreeView().kind());
export { CardsView, TreeView };

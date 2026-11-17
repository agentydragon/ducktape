const viewRegistry = {
  register(x) {
    (this.v ||= []).push(x);
  },
};
const commandRegistry = {
  register(x) {
    (this.c ||= []).push(x);
  },
};
class TableView {}
class DeleteCommand {}
viewRegistry.register(TableView);
commandRegistry.register(DeleteCommand);
console.log(new TableView().constructor.name, new DeleteCommand().constructor.name);
export { viewRegistry, commandRegistry, TableView, DeleteCommand };

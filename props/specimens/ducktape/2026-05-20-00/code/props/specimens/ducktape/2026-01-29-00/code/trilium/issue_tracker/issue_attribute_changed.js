api.log(`api.originEntity: ${JSON.stringify(api.originEntity)}`);

const note = api.originEntity.getNote();

api.log(`origin entity ${JSON.stringify(api.originEntity)}`);
api.log(`issue attribute changed ${note.noteId}, ${api.originEntity.name}, ${api.originEntity.value}`);

if (api.originEntity.name == "state" && !api.originEntity.isDeleted) {
  const stateNote = api.getNote(api.originEntity.value);
  api.log(`state in api.originEntity ${api.originEntity.value} ${stateNote.title}`);
  // If state sets an icon, set it on the issue.
  if (stateNote.hasLabel("issueIcon")) {
    note.setLabel("iconClass", stateNote.getLabel("issueIcon").value);
  }
}

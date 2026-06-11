// install as frontent JS with attributes: #run=frontendStartup
// #run=mobileStartup

api.addButtonToToolbar({
  title: "Add repeating note for today",
  icon: "notepad",
  shortcut: "alt+n",
  action: async () => {
    // creating notes is backend (server) responsibility so we need to pass
    // the control there
    const newNoteId = await api.runOnBackend(() => {
      const today = api.getTodayNote();
      // TODO: set repeating note's title based on day & stuff
      const resp = api.createTextNote(today.noteId, "new repeating note", "");
      const newNote = resp.note;

      const repeatingNoteTemplateNoteId = "piDc86W245NG";
      // const openStateNoteId = api.getNoteWithLabel("issueStateOpen").noteId;
      newNote.setRelation("template", repeatingNoteTemplateNoteId);
      newNote.setRelation("date", today.noteId);

      return newNote.noteId;
    });

    // wait until the frontend is fully synced with the changes made on the
    // backend above
    await api.waitUntilSynced();

    // we got an ID of newly created note and we want to immediately display it
    // TODO: activate it in the path under the active tab, not under tasks where
    // it's been created
    await api.activateNewNote(newNoteId);
  },
});

const TPL = `
  <h2>States</h2>
  <ul class='state-list'></ul>
  <h2>Hotlists</h2>
  <ul class='hotlist-list'></ul>
  <table>
  <thead>
    <tr>
      <th>Issue</th>
      <th>Hotlists</th>
    </tr>
  </thead>
  <tbody class="issue-list">
  </tbody>
  </table>`;

class IssueTable {
  constructor() {
    this.hotlistIncludeIds = [];
    this.hotlistExcludeIds = [];
    // TODO: by default only show open issues
    this.stateIds = [];
    this.$issueList = null;
    this.$hotlistList = null;
    this.$stateList = null;
  }

  getHotlists() {
    return api.runOnBackend(() => {
      return api.searchForNotes("#hotlist").map((note) => {
        return { noteId: note.noteId, title: note.title };
      });
    });
  }

  getStates() {
    return api.runOnBackend(() => {
      const statesRoot = api.searchForNotes("#issueStatesRoot")[0];
      const stateNotes = statesRoot.getChildNotes();
      return stateNotes.map((note) => {
        return { noteId: note.noteId, title: note.title };
      });
    });
  }

  getHotlistsForIssue(issueId) {
    return api.runOnBackend(
      (issueId) => {
        const issue = api.getNote(issueId);
        const hotlists = issue.getRelations("hotlist");
        return hotlists.map((hotlist) => {
          return hotlist.value;
        });
      },
      [issueId]
    );
  }

  getIssues() {
    let searchString = "#issue";
    if (this.stateIds && this.stateIds.length > 0) {
      searchString += " AND ";
      if (this.stateIds.length > 1) {
        searchString += "(";
      }
      searchString += this.stateIds.map((stateId) => "~state.noteId=" + stateId).join(" OR ");
      if (this.stateIds.length > 1) {
        searchString += ")";
      }
    }
    if (this.hotlistIncludeIds.length > 0) {
      searchString += " AND ";
      if (this.hotlistIncludeIds.length > 1) {
        searchString += "(";
      }
      searchString += this.hotlistIncludeIds.map((hotlistId) => "~hotlist.noteId=" + hotlistId).join(" OR ");
      if (this.hotlistIncludeIds.length > 1) {
        searchString += ")";
      }
    }
    if (this.hotlistExcludeIds.length > 0) {
      searchString += " AND NOT (";
      searchString += this.hotlistExcludeIds.map((hotlistId) => "~hotlist.noteId=" + hotlistId).join(" OR ");
      searchString += ")";
    }
    console.log("search string", searchString);

    return api.runOnBackend(
      (searchString) => {
        const notes = api.searchForNotes(searchString);
        return notes.map((note) => {
          return { noteId: note.noteId, title: note.title };
        });
      },
      [searchString]
    );
  }

  async createIssueLink(note) {
    return await api.createNoteLink(note.noteId, { showTooltip: true, showNoteIcon: true });
  }

  async fillHotlistTable() {
    this.$hotlistList.empty();
    const hotlists = await this.getHotlists();
    hotlists.sort((a, b) => a.title.localeCompare(b.title));
    for (const note of hotlists) {
      const item = $("<li>");
      item.attr("data-hotlist-id", note.noteId);

      const link = $('<a href="#">')
        .text(note.title)
        .click(() => this.toggleHotlist(note.noteId));
      item.append(link);
      this.$hotlistList.append(item);
    }
  }

  async fillStateTable() {
    this.$stateList.empty();
    for (const state of await this.getStates()) {
      const item = $("<li>");
      item.attr("data-state-id", state.noteId);

      const link = $('<a href="#">')
        .text(state.title)
        .click(() => this.selectState(state.noteId));
      item.append(link);
      this.$stateList.append(item);
    }
  }

  async fillIssueTable() {
    this.$issueList.empty();
    const promises = [];
    const issues = await this.getIssues();
    for (const note of issues) {
      const issueCell = $("<td>");
      const hotlistsCell = $("<td>");

      promises.push(
        this.createIssueLink(note).then((issueLink) => {
          issueCell.append(issueLink);
        })
      );

      promises.push(
        this.getHotlistsForIssue(note.noteId).then((hotlistIds) => {
          return Promise.all(
            hotlistIds.map((hotlistId) =>
              api
                .createNoteLink(hotlistId, { showTooltip: true, showNoteIcon: true })
                .then((hotlistLink) => hotlistsCell.append(hotlistLink))
            )
          );
        })
      );

      const row = $("<tr>").append(issueCell).append(hotlistsCell);
      this.$issueList.append(row);
    }
  }

  async toggleHotlist(hotlistId) {
    this.$hotlistList.find("[data-hotlist-id]").removeClass("active");
    if (this.hotlistIncludeIds.includes(hotlistId)) {
      this.hotlistIncludeIds = this.hotlistIncludeIds.filter((id) => id !== hotlistId);
      this.hotlistExcludeIds.push(hotlistId);
      this.$hotlistList.find("[data-hotlist-id=" + hotlistId + "]").addClass("excluded");
    } else if (this.hotlistExcludeIds.includes(hotlistId)) {
      this.hotlistExcludeIds = this.hotlistExcludeIds.filter((id) => id !== hotlistId);
      this.$hotlistList.find("[data-hotlist-id=" + hotlistId + "]").removeClass("excluded");
    } else {
      this.hotlistIncludeIds.push(hotlistId);
      this.$hotlistList.find("[data-hotlist-id=" + hotlistId + "]").addClass("active");
    }
    await this.fillIssueTable();
  }

  async selectState(stateId) {
    this.$stateList.find("[data-state-id=" + stateId + "]").toggleClass("active");
    if (this.stateIds.includes(stateId)) {
      this.stateIds = this.stateIds.filter((id) => id !== stateId);
    } else {
      this.stateIds.push(stateId);
    }
    await this.fillIssueTable();
  }

  async render(root) {
    this.$root = root;
    this.$root.empty().append($(TPL));
    this.$issueList = this.$root.find(".issue-list");
    this.$hotlistList = this.$root.find(".hotlist-list");
    this.$stateList = this.$root.find(".state-list");

    await this.fillHotlistTable();
    await this.fillStateTable();
    await this.fillIssueTable();
  }
}

const issueTable = new IssueTable();
await issueTable.render($("#issue-table-root"));

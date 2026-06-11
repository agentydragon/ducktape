/**
 * Hotlist sidebar widget.
 *
 * To install, add as a note of type "JS frontend" and add #widget label.
 */

const TPL = `
<div>
  Open issues in this hotlist:
  <ul class="hotlist-issue-list">
  </ul>
</div>`;

class HotlistSidebarWidget extends api.CollapsibleWidget {
  get position() {
    return 10;
  }
  get parentWidget() {
    return "right-pane";
  }
  get widgetTitle() {
    return "Issue";
  }

  async doRenderBody() {
    this.$body.empty().append($(TPL));
    this.$issueList = this.$body.find(".hotlist-issue-list");
    return this.$body;
  }

  isEnabled() {
    return super.isEnabled() && this.note.type === "text" && this.note.hasLabel("hotlist");
  }

  async refreshWithNote(note) {
    let searchString = "#issue";
    searchString += " ~state.title=Open";
    searchString += " ~hotlist.noteId=" + note.noteId;

    let issueNotes = await api.searchForNotes(searchString);
    this.$issueList.empty();
    // TODO: sort them nicely
    // TODO: some more info - also show done issues etc.; generally group by
    // state
    for (const issueNote of issueNotes) {
      const bullet = $("<li>");
      bullet.append(await api.createNoteLink(issueNote.noteId, { showTooltip: true }));
      this.$issueList.append(bullet);
    }
  }

  async entitiesReloadedEvent({ loadResults }) {
    // TODO: how about changes elsewhere...? is that included?
    if (loadResults.isNoteContentReloaded(this.noteId)) {
      this.refresh();
    }
  }
}

module.exports = new HotlistSidebarWidget();

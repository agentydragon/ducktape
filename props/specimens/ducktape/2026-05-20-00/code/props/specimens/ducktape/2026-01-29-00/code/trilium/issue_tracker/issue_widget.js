/**
 * Issue widget. For issue notes, allows changing state and shows attached
 * hotlists.
 *
 * Expects OpenAI API key saved in a plaintext code note with #openaiApiKey
 * label.
 */

const TPL = `<div>
  <div id="issue-state-buttons"></div>
  <ul id="issue-hotlists"></ul>
  <button id="suggest-hotlists-button">Suggest hotlists</button>
  <details><summary>Prompt</summary><output id="prompt-output"></output></details>
  <ul id="suggested-hotlists"></ul>
</div>`;

class IssueWidget extends api.CollapsibleWidget {
  get position() {
    return 100;
  }
  get parentWidget() {
    return "right-pane";
  }
  get widgetTitle() {
    return "Issue";
  }

  isEnabled() {
    return super.isEnabled() && this.note.type === "text" && this.note.hasLabel("issue");
  }

  async doRenderBody() {
    this.$body.empty().append($(TPL));
    this.$stateButtons = this.$body.find("#issue-state-buttons");
    this.$hotlists = this.$body.find("#issue-hotlists");
    this.$promptOutput = this.$body.find("#prompt-output");
    this.$suggestHotlistsButton = this.$body.find("#suggest-hotlists-button");
    this.$suggestedHotlists = this.$body.find("#suggested-hotlists");

    this.$suggestHotlistsButton.click(() => {
      this.suggestHotlists();
    });

    return this.$body;
  }

  async setState(stateId) {
    await api.runOnBackend(
      async (noteId, stateId) => {
        const note = await api.getNote(noteId);
        note.setRelation("state", stateId);
      },
      [this.note.noteId, stateId]
    );
  }

  async getStatesRootNoteId() {
    const results = await api.searchForNotes("#issueStatesRoot");
    if (results.length > 0) {
      return results[0];
    } else {
      throw new Error("Issue states root note not found.");
    }
  }

  async makePrompt() {
    // TODO: also show OpenAI the existing hierarchy of hotlists
    // TODO: also include hotlist description if available
    let openaiPrompt = `
In my issue tracker, I have issues into hotlists. I'll give you the list of
hotlists I already have, identified by an index, and the title of an issue.
Suggest a couple existing hotlists that might be relevant to the issue out of
the list, and identify them by index. Optionally you may also suggest a new
hotlist I might want to create for this issue.

Existing hotlists:
`;
    const hotlistNotes = await this.getHotlistNotes();
    for (let i = 0; i < hotlistNotes.length; ++i) {
      const hotlistNote = hotlistNotes[i];
      openaiPrompt += `${i}: ${hotlistNote.title}\n`;
      // TODO: also fetch parent hotlists, for OpenAI
    }
    openaiPrompt += "\n";
    openaiPrompt += `Issue title: ${this.note.title}\n`;
    const hotlistRelations = this.note.getRelations("hotlist");
    if (hotlistRelations.length > 0) {
      openaiPrompt += "The issue is already in the following hotlists - do not suggest them: ";
      const titles = [];
      for (const hotlistRelation of hotlistRelations) {
        const hotlistNote = await api.getNote(hotlistRelation.value);
        titles.push(hotlistNote.title);
      }
      openaiPrompt += titles.join(", ");
    }
    openaiPrompt += "\n";
    openaiPrompt +=
      "\nSuggest hotlists for this issue. Prefer suggesting hotlists that I already have, but if it makes sense, you may also suggest a new hotlist.\n";
    openaiPrompt +=
      "Format the suggestion as a JSON array like this: " + '["Hotlist name 1","Hotlist name 2",...].\n' + "-----\n";
    return openaiPrompt;
  }

  async refreshWithNote(note) {
    const statesRoot = await this.getStatesRootNoteId();
    const stateNoteIds = statesRoot.getChildNoteIds();
    this.$stateButtons.empty();
    const currentState = await note.getRelationTarget("state");
    const currentStateId = currentState ? currentState.noteId : null;
    for (const stateNoteId of stateNoteIds) {
      const stateNote = await api.getNote(stateNoteId);
      const button = $("<button></button>");
      button.text(stateNote.title);
      button.data("state-id", stateNoteId);
      button.on("click", async () => {
        await this.setState(button.data("state-id"));
      });
      if (stateNote.hasLabel("issueIcon")) {
        const icon = $("<i></i>");
        icon.addClass(stateNote.getLabel("issueIcon").value);
        button.prepend(icon);
      }
      if (currentStateId === stateNoteId) {
        button.prop("disabled", true);
      }
      this.$stateButtons.append(button);
    }

    this.$hotlists.empty();
    const hotlistRelations = note.getRelations("hotlist");
    for (const hotlistRelation of hotlistRelations) {
      const hotlistNote = await api.getNote(hotlistRelation.value);
      if (hotlistNote) {
        const listItem = $("<li></li>");
        listItem.append(await api.createNoteLink(hotlistNote.noteId, { showTooltip: true, showNoteIcon: true }));
        this.$hotlists.append(listItem);
      }
    }

    // TODO: also show OpenAI the existing hierarchy of hotlists
    // TODO: also include hotlist description if available
    // TODO: fetch all hotlists

    // TODO: should instead show the thing
  }

  async suggestHotlists() {
    const OPENAI_API_KEY = await api.runOnBackend(() => {
      return api.searchForNote("#openaiApiKey").getContent();
    }, []);
    console.log("OPENAI API KEY:", OPENAI_API_KEY);
    const prefix = '["';
    const openaiPrompt = (await this.makePrompt()) + prefix;

    this.$promptOutput.text(openaiPrompt);

    $.ajax({
      url: "https://api.openai.com/v1/completions",
      type: "POST",
      data: JSON.stringify({
        prompt: openaiPrompt,
        max_tokens: 2048,
        temperature: 0.5,
        model: "text-davinci-003",
      }),
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ` + OPENAI_API_KEY,
      },
      success: async (response) => {
        // console.log(response.choices[0].text);
        await this.showSuggestions(JSON.parse(prefix + response.choices[0].text));
      },
      error: (error) => {
        alert("error, see console");
        console.log(error);
      },
    });
    // TODO: have an OpenAI prompt -> response cache somewhere
  }

  async getHotlistNotes() {
    return await api.searchForNotes("#hotlist");
  }

  async showSuggestions(jsonObject) {
    const hotlistNotes = await this.getHotlistNotes();

    const hotlistTitleToIdMap = new Map();
    for (const hotlistNote of hotlistNotes) {
      hotlistTitleToIdMap.set(hotlistNote.title, hotlistNote.noteId);
    }

    // TODO: for each hotlist, add a button to add relation.
    // sort in order: 1. recommended hotlists in that order, 2. not
    // recommended. click -> add hotlist relation.
    this.$suggestedHotlists.empty();
    const alreadyDisplayed = new Set();
    for (const hotlistTitle of jsonObject) {
      if (hotlistTitleToIdMap.has(hotlistTitle)) {
        const hotlistId = hotlistTitleToIdMap.get(hotlistTitle);
        alreadyDisplayed.add(hotlistId);
        const bullet = $("<li>");
        const button = $("<button>");
        button.text("+ ");
        const hotlistNote = await api.getNote(hotlistId);
        if (hotlistNote.hasLabel("iconClass")) {
          const icon = $("<i></i>");
          icon.addClass(hotlistNote.getLabel("iconClass").value);
          button.append(icon);
        }
        button.append(hotlistTitle);
        button.click(async () => {
          // add the relation; TODO: should also re-render...
          console.log("adding...");
          await api.runOnBackend(
            (issueId, hotlistId) => {
              api.getNote(issueId).addAttribute("relation", "hotlist", hotlistId);
            },
            [this.noteId, hotlistId]
          );
          console.log("added...");
        });

        bullet.append(button);
        this.$suggestedHotlists.append(bullet);
      }
    }
    for (const hotlistTitle of jsonObject) {
      if (!hotlistTitleToIdMap.has(hotlistTitle)) {
        const bullet = $("<li>");
        bullet.text(` Suggested: ${hotlistTitle}`);
        this.$suggestedHotlists.append(bullet);
      }
    }

    // TODO: show other hotlists, allow for adding
  }

  async entitiesReloadedEvent({ loadResults }) {
    if (
      loadResults.isNoteContentReloaded(this.noteId) ||
      loadResults
        .getAttributes()
        .find((attr) => attr.type === "relation" && (attr.name === "state" || attr.name == "hotlist"))
    ) {
      this.refresh();
    }
  }
}

module.exports = new IssueWidget();

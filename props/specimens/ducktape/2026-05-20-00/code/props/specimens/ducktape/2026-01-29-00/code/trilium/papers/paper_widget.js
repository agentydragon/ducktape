/**
 * Paper widget. Allows adding, managing topics.
 *
 * Expects OpenAI API key saved in a plaintext code note with #openaiApiKey
 * label.
 *
 * TODO: refactor the suggestion into a more general thing
 */

const TPL = `<div>
  <ul id="paper-topics"></ul>
  <button id="suggest-topics-button">Suggest topics</button>
  <details><summary>Prompt</summary><output id="prompt-output"></output></details>
  <ul id="suggested-topics"></ul>
</div>`;

class PaperWidget extends api.CollapsibleWidget {
  get position() {
    return 100;
  }
  get parentWidget() {
    return "right-pane";
  }
  get widgetTitle() {
    return "Paper";
  }

  isEnabled() {
    return super.isEnabled() && this.note.type === "text" && this.note.hasLabel("paper");
  }

  async doRenderBody() {
    this.$body.empty().append($(TPL));
    this.$paperTopicsList = this.$body.find("#paper-topics");
    this.$promptOutput = this.$body.find("#prompt-output");
    this.$suggestTopicsButton = this.$body.find("#suggest-topics-button");
    this.$suggestedTopics = this.$body.find("#suggested-topics");

    this.$suggestTopicsButton.click(() => {
      this.suggestTopics();
    });

    return this.$body;
  }

  async makePrompt() {
    // TODO: also show OpenAI the existing hierarchy of topics
    let openaiPrompt = `
In my database of papers to read, I group papers by topics. I'll give you the
list of topics I already have, identified by an index, and the title of a paper.
Suggest a couple existing topics that might be relevant to the paper out of
the list, and identify them by index. Optionally you may also suggest a new
topic I might want to create for this paper.

Existing topics:
`;
    const topicNotes = await this.getTopicNotes();
    for (let i = 0; i < topicNotes.length; ++i) {
      const topicNote = topicNotes[i];
      openaiPrompt += `${i}: ${topicNote.title}\n`;
      // TODO: also fetch parent topics, for OpenAI
    }
    openaiPrompt += "\n";
    openaiPrompt += `Paper title: ${this.note.title}\n`;
    const topicRelations = this.note.getRelations("topic");
    if (topicRelations.length > 0) {
      openaiPrompt += "The paper is already assigned to the following topics - do not suggest them: ";
      const titles = [];
      for (const topicRelation of topicRelations) {
        const topicNote = await api.getNote(topicRelation.value);
        titles.push(topicNote.title);
      }
      openaiPrompt += titles.join(", ");
    }
    openaiPrompt += "\n";
    openaiPrompt +=
      "\nSuggest topics for this paper. Prefer suggesting topics that I already have, but if it makes sense, you may also suggest a new topic.\n";
    openaiPrompt +=
      "Format the suggestion as a JSON array like this: " + '["Topic name 1","Topic name 2",...].\n' + "-----\n";
    return openaiPrompt;
  }

  async refreshWithNote(note) {
    this.$paperTopicsList.empty();
    const topicRelations = note.getRelations("topic");
    for (const topicRelation of topicRelations) {
      const topicNote = await api.getNote(topicRelation.value);
      if (!topicNote) {
        continue;
      }
      const listItem = $("<li>");
      listItem.append(await api.createNoteLink(topicNote.noteId, { showTooltip: true, showNoteIcon: true }));
      this.$paperTopicsList.append(listItem);
    }

    // TODO: also show OpenAI the existing hierarchy of topics
    // TODO: fetch all topics

    // TODO: should instead show the thing
  }

  async suggestTopics() {
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

  async getTopicNotes() {
    return await api.searchForNotes("#topic");
  }

  async showSuggestions(jsonObject) {
    const topicNotes = await this.getTopicNotes();

    const topicTitleToIdMap = new Map();
    for (const topicNote of topicNotes) {
      // normalize to lower case in case model outputs upper case
      topicTitleToIdMap.set(topicNote.title.toLowerCase(), topicNote.noteId);
    }

    // TODO: for each topic, add a button to add relation.
    // sort in order: 1. recommended topics in that order, 2. not
    // recommended. click -> add topic relation.
    this.$suggestedTopics.empty();
    const alreadyDisplayed = new Set();
    for (const topicTitle of jsonObject) {
      const normalizedTitle = topicTitle.toLowerCase();
      if (!topicTitleToIdMap.has(normalizedTitle)) {
        continue;
      }
      const topicId = topicTitleToIdMap.get(normalizedTitle);
      alreadyDisplayed.add(topicId);
      const bullet = $("<li>");
      const button = $("<button>");
      button.text("+ ");
      const topicNote = await api.getNote(topicId);
      if (topicNote.hasLabel("iconClass")) {
        const icon = $("<i></i>");
        icon.addClass(topicNote.getLabel("iconClass").value);
        button.append(icon);
      }
      button.append(topicTitle);
      button.click(async () => {
        // add the relation; TODO: should also re-render...
        console.log("adding...");
        await api.runOnBackend(
          (paperId, topicId) => {
            api.getNote(paperId).addAttribute("relation", "topic", topicId);
          },
          [this.noteId, topicId]
        );
        console.log("added...");
      });

      bullet.append(button);
      this.$suggestedTopics.append(bullet);
    }
    for (const topicTitle of jsonObject) {
      const normalizedTitle = topicTitle.toLowerCase();
      if (topicTitleToIdMap.has(normalizedTitle)) {
        continue;
      }
      const bullet = $("<li>");
      bullet.text(` Suggested: ${topicTitle}`);
      this.$suggestedTopics.append(bullet);
    }
  }

  // TODO: show other topics, allow for adding

  async entitiesReloadedEvent({ loadResults }) {
    if (
      loadResults.isNoteContentReloaded(this.noteId) ||
      loadResults
        .getAttributes()
        .find((attr) => attr.type === "relation" && (attr.name === "state" || attr.name == "topic"))
    ) {
      this.refresh();
    }
  }
}

module.exports = new PaperWidget();

/**
 * To install, add as frontend script and add #widget.
 */

const TPL = `<div>
  <form id="arxiv-form">
    <input type="text" id="arxiv-input" placeholder="arXiv ID or URL" style="width: 80%;">
    <button type="submit">Add</button>
  </form>
  <output id="arxiv-message" style="color: var(--main-text-color);"></output>
</div>`;

const ARXIV_ENDPOINT = "https://export.arxiv.org/api/query";
const PAPER_TEMPLATE_NOTE_ID = "WgCQiTGFyKV7";
const PAPERS_ROOT_LABEL = "papersRoot";

class ArxivWidget extends api.CollapsibleWidget {
  get position() {
    return 20;
  }
  get parentWidget() {
    return "right-pane";
  }
  get widgetTitle() {
    return "Add arXiv paper";
  }

  async doRenderBody() {
    this.$body.empty().append($(TPL));
    this.$input = this.$body.find("#arxiv-input");
    this.$form = this.$body.find("#arxiv-form");
    this.$message = this.$body.find("#arxiv-message");

    // Make message disappear when URL is updated.
    this.$input.on("input", () => this.$message.text(""));

    this.$form.on("click", async () => {
      const urlToAdd = this.$input.val().trim();
      if (!urlToAdd) {
        this.$message.text("Please enter a valid arXiv ID or URL.");
        return;
      }
      try {
        this.$message.text("Adding paper...");
        await this.addPaper(urlToAdd);
      } catch (e) {
        console.error(e, e.stack);
        this.$message.text("Error: " + e.message + " " + e.stack);
      }
    });
    return this.$body;
  }

  async addPaper(urlToAdd) {
    // parse the paper ID from the input
    let paperId = this.parsePaperId(urlToAdd);

    // check if the paper already exists in Trilium
    const existingPaper = await this.findNoteByPaperId(paperId);
    if (existingPaper) {
      await this.showExistingPaperMessage(existingPaper);
      return;
    }

    // fetch the paper metadata from arXiv
    const meta = await this.getPaperMeta(paperId);

    // search for notes with similar titles
    const title = meta.title;
    // TODO: maybe show links to all similar pages, not just one

    // Do not offer to auto-link to stuff that already has an arxiv ID
    const query = title + " #-arxivId";
    // TODO: maybe search fuzzily, skipping individual words
    const results = await api.searchForNotes(title);
    if (results.length > 0) {
      // show a message with a link to the note and a confirmation button
      await this.showSimilarNotesMessage(title, results, paperId);
      return;
    }

    // create a new note for the paper on the backend
    let newNote = await this.addNoteToBackend(title, paperId);
    await this.showNewPaperMessage(newNote);
  }

  parsePaperId(arxivUrl) {
    if (/^\d{4}\.\d{4,5}$/.test(arxivUrl)) {
      return arxivUrl;
    } else {
      const match = arxivUrl.match(/arxiv\.org\/(?:abs|pdf)\/(\d{4}\.\d{4,5})(?:v\d+)?(?:\.pdf)?/);
      if (match) {
        return match[1];
      } else {
        throw new Error("Invalid arXiv ID or URL.");
      }
    }
  }

  async showExistingPaperMessage(existingPaper) {
    this.$message.text("Paper already exists as: ");
    const noteLink = await api.createNoteLink(existingPaper.noteId, {
      showTooltip: true,
      showNoteIcon: true,
    });
    this.$message.append(noteLink);
  }

  async showSimilarNotesMessage(title, results, paperId) {
    this.$message.text(results.length + " notes with similar title found:");

    // add a button for each similar note
    results.forEach(async (result) => {
      const noteLink = await api.createNoteLink(result.noteId, {
        showTooltip: true,
        showNoteIcon: true,
      });
      this.$message.append(
        $("<div>").append(
          noteLink,
          $("<button>")
            .text("Link to ArXiv")
            .on("click", async () => {
              // add the paper ID attribute to the existing note
              await api.runOnBackend(
                function (noteId, paperId) {
                  const note = api.getNote(noteId);
                  note.addLabel("arxivId", paperId);
                },
                [result.noteId, paperId]
              );
              // show a message indicating that the note was linked
              this.$message.append(
                $("<div>").append(
                  "Paper ID added to: ",
                  await api.createNoteLink(result.noteId, { showTooltip: true, showNoteIcon: true })
                )
              );
              // clear the input field
              this.$input.val("");
            })
        )
      );
    });
    this.$message.append("<br>Confirm to create a new note anyway: " + '<button id="arxiv-confirm">Confirm</button>');
    this.$body.find("#arxiv-confirm").on("click", async () => {
      // create the note as usual
      let newNote = await this.addNoteToBackend(title, paperId);
      this.showNewPaperMessage(newNote);
    });
  }

  async showNewPaperMessage(newNote) {
    this.$message.text("Paper added as: ");
    this.$message.append(await api.createNoteLink(newNote.noteId, { showTooltip: true, showNoteIcon: true }));
  }

  async addNoteToBackend(title, paperId) {
    const papersRoot = await this.getPapersRootNoteId();
    // Will run on backend
    const backendFn = (papersRootNoteId, title, paperId, paperTemplateNoteId) => {
      const newNote = api.createTextNote(papersRootNoteId, title, "auto-created for paper ID " + paperId).note;
      newNote.addRelation("template", paperTemplateNoteId);
      newNote.addLabel("arxivId", paperId);
      return newNote;
    };
    const newNote = await api.runOnBackend(backendFn, [papersRoot.noteId, title, paperId, PAPER_TEMPLATE_NOTE_ID]);
    return newNote;
  }

  async findNoteByPaperId(paperId) {
    // search for notes with the arxivId label
    const results = await api.searchForNotes('#arxivId = "' + paperId + '"');
    if (results.length > 0) {
      return results[0];
    } else {
      return null;
    }
  }

  async getPaperMeta(paperId) {
    // query the arXiv API for the paper details
    const url = ARXIV_ENDPOINT + "?" + $.param({ id_list: paperId });
    const response = await $.get(url);
    const xml = response;
    const entry = $(xml).find("entry");
    if (entry.length > 0) {
      const title = this.sanitizeTitle(entry.find("title").text());
      return { title };
    } else {
      throw new Error("Paper not found on arXiv.");
    }
  }

  sanitizeTitle(title) {
    // remove extra whitespace and line breaks from the title
    return title.replace(/\s+/g, " ").trim();
  }

  async getPapersRootNoteId() {
    // search for the note with the papersRoot label
    const results = await api.searchForNotes("#" + PAPERS_ROOT_LABEL);
    if (results.length > 0) {
      return results[0];
    } else {
      throw new Error("Papers root note not found.");
    }
  }
}

module.exports = new ArxivWidget();

const TPL = `
  <h2>Papers</h2>
  sort by ascending priority<br>
  TODO: show associated topics, allow filtering<br>
  TODO: show number of papers that cite this paper<br>
  TODO: show date published (or just year if not known exactly)<br>
  <output class="controls"></output>
  <output class="topics"></output>
  <table>
  <thead>
    <tr>
      <th rowspan=2>Paper</th>
      <th rowspan=2>arXiv</th>
      <th rowspan=1 colspan=2>Priority</th>
      <th rowspan=2>Topic</th>
    </tr>
    <tr>
      <th>Priority</th>
      <th>Updated</th>
    </tr>
  </thead>
  <tbody class="paper-list">
  </tbody>
  </table>`;
const INCLUDE = "include";
const EXCLUDE = "exclude";
const ONLY = "only";
const OPTIONS = [INCLUDE, EXCLUDE, ONLY];

class PaperTable {
  constructor() {
    this.$root = null;
    this.$paperList = null;
    this.$topicList = null;
    this.$controls = null;

    this.finishedReadingMode = EXCLUDE;
    this.unprioritizedMode = INCLUDE;
  }

  async fillTopicList() {
    this.$topicList.empty();

    const topicNotes = await api.searchForNotes("#topic");
    for (const topic of topicNotes) {
      // noteId, title
      this.$topicList.append(topic.title);
      // TODO: would be pretty nice to do this reactively...
    }
  }

  buildPaperRow(row) {
    const paperCell = $("<td>");
    const arxivLinkageCell = $("<td>");
    api.getNote(row.noteId).then((note) => {
      const arxivId = note.getAttribute("label", "arxivId");
      if (arxivId && arxivId !== "") {
        arxivLinkageCell.text("✓");
      } else {
        arxivLinkageCell.text("✗");
      }
    });
    const priorityCell = $("<td>");
    const priorityInput = $("<input type=number>").val(row.priority);
    priorityInput.change(async () => {
      const newPriority = priorityInput.val();
      // console.log(newPriority, typeof(newPriority));

      if (newPriority === "") {
        // TODO: deprioritize
        console.log("TODO: not changing");
      } else if (typeof (newPriority === "string")) {
        // console.log(`setting priority of ${row.noteId} to ${newPriority}`);
        await api.runOnBackend(
          async (noteId, newPriorityInner) => {
            const note = await api.getNote(noteId);
            note.setLabel("readingPriority", newPriorityInner);
            note.setLabel("readingPriorityDate", new Date().toISOString().substring(0, 10));
          },
          [row.noteId, newPriority]
        );
      }
    });

    // TODO: this promise should be cancelled if rendering is interrupted
    api.createNoteLink(row.noteId, { showTooltip: true, showNoteIcon: true }).then((paperLink) => {
      paperCell.append(paperLink);
    });
    priorityCell.append(priorityInput);
    const priorityDateCell = $("<td>").text(row.priorityDate);
    const topicsCell = $("<td>");
    api.getNote(row.noteId).then((note) => {
      const topics = note.getAttributes("relation", "topic");
      for (const topic of topics) {
        // TODO: toggle-button for this topic; also suggest a couple
        // topics, have a combo box for search
        api.createNoteLink(topic.value).then((topicLink) => {
          topicsCell.append(topicLink);
        });
      }
    });
    const rowElement = $("<tr>").append(paperCell, arxivLinkageCell, priorityCell, priorityDateCell, topicsCell);
    return rowElement;
  }

  // TODO: variable limit
  async fillPaperList() {
    // TODO: pagination...
    this.$paperList.empty();

    // TODO: check there's up to 1 attribute for each paper
    let sql = `
      SELECT DISTINCT
        Papers.noteId,
        Papers.title,
        Priority.priority,
        PriorityDate.priorityDate,
        CASE Finished.finishedReading
          WHEN 'true' THEN 1
          ELSE 0 END AS finishedReading
      FROM (
        SELECT
          notes.noteId,
          notes.title
        FROM
          notes
          LEFT JOIN attributes USING (noteId)
        WHERE
          NOT notes.isDeleted
          AND attributes.name = 'template'
          AND attributes.value = 'WgCQiTGFyKV7'
      ) AS Papers LEFT JOIN (
        SELECT
          attributes.noteId,
          CAST(attributes.value AS INTEGER) AS priority
        FROM
          attributes
        WHERE
          NOT attributes.isDeleted
          AND attributes.name = 'readingPriority'
      ) AS Priority USING (noteId)
      LEFT JOIN (
        SELECT
          attributes.noteId,
          attributes.value AS priorityDate
        FROM
          attributes
        WHERE
          NOT attributes.isDeleted
          AND attributes.name = 'readingPriorityDate'
      ) AS PriorityDate USING (noteId)
      LEFT JOIN (
        SELECT
          attributes.noteId,
          attributes.value AS finishedReading
        FROM
          attributes
        WHERE
          NOT attributes.isDeleted
          AND attributes.name = 'finishedReading'
      ) AS Finished USING (noteId)
      WHERE TRUE
    `;

    if (this.finishedReadingMode == INCLUDE) {
    } else if (this.finishedReadingMode == EXCLUDE) {
      // TODO: do this on the 0/1 level
      sql += `AND NOT (Finished.finishedReading = 'true') OR Finished.finishedReading IS NULL `;
    } else if (this.finishedReadingMode == ONLY) {
      sql += `AND (Finished.finishedReading = 'true') `;
    }

    if (this.unprioritizedMode == INCLUDE) {
    } else if (this.unprioritizedMode == EXCLUDE) {
      // TODO: do this on the 0/1 level
      sql += `AND Priority.priority IS NOT NULL`;
    } else if (this.unprioritizedMode == ONLY) {
      sql += `AND Priority.priority IS NULL`;
    }

    sql += `
      ORDER BY priority ASC
    `;
    // TODO: variable limit
    sql += "LIMIT 100";

    const rows = await api.runOnBackend(
      (sql) => {
        return api.sql.getRows(sql);
      },
      [sql]
    );
    for (const row of rows) {
      this.$paperList.append(this.buildPaperRow(row));
    }
  }

  fillControls() {
    this.$controls.empty();

    const finishedReadingButton = $("<input type=button>");
    finishedReadingButton.val("Finished reading: " + this.finishedReadingMode);
    finishedReadingButton.click(async () => {
      const oldIndex = OPTIONS.indexOf(this.finishedReadingMode);
      const newIndex = (oldIndex + 1) % OPTIONS.length;
      this.finishedReadingMode = OPTIONS[newIndex];
      finishedReadingButton.val("Finished reading: " + this.finishedReadingMode);
      await this.fillPaperList();
    });
    this.$controls.append(finishedReadingButton);

    const unprioritizedButton = $("<input type=button>");
    unprioritizedButton.val("Unprioritized: " + this.unprioritizedMode);
    unprioritizedButton.click(async () => {
      const oldIndex = OPTIONS.indexOf(this.unprioritizedMode);
      const newIndex = (oldIndex + 1) % OPTIONS.length;
      this.unprioritizedMode = OPTIONS[newIndex];
      unprioritizedButton.val("Unprioritized: " + this.unprioritizedMode);
      await this.fillPaperList();
    });
    this.$controls.append(unprioritizedButton);
  }

  async render(root) {
    this.$root = root;
    this.$root.empty().append($(TPL));
    this.$paperList = this.$root.find(".paper-list");
    this.$controls = this.$root.find(".controls");
    this.$topicList = this.$root.find(".topics");

    this.fillControls();
    // TODO: cancel promises if rendering aborted / re-render triggered
    await Promise.all([this.fillPaperList(), this.fillTopicList()]);
  }
}

const paperTable = new PaperTable();
await paperTable.render($("#paper-table-root"));

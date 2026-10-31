// @vitest-environment happy-dom
import { act } from "react";
import { describe, expect, it } from "vitest";

import { mount } from "../../testing/harness";
import {
  actionCompactCardExpands,
  actionOffersCardDecisionButtons,
  renderActionLabel,
  renderDetailsArguments,
  renderPaneCollapsed,
  renderPaneOpened,
} from "../index";

const PUSH_FILES = { group: "github", name: "push_files" };
const DELETE_FILE = { group: "github", name: "delete_file" };
const UPDATE_PULL_REQUEST = { group: "github", name: "update_pull_request" };
const CREATE_BRANCH = { group: "github", name: "create_branch" };
const CREATE_PULL_REQUEST = { group: "github", name: "create_pull_request" };
const SEARCH_PULL_REQUESTS = { group: "github", name: "search_pull_requests" };

const REPOSITORY = { owner: "test-owner", repo: "test-repo" };

/** The text a widget drew, without the style sheet `MantineProvider` adds to the mount. */
function shownText(container: HTMLElement): string {
  const copy = container.cloneNode(true) as HTMLElement;
  for (const style of copy.querySelectorAll("style")) style.remove();
  return copy.textContent?.trim() ?? "";
}

/** Each file block's summary and, once opened, its text. */
function fileBlocks(container: HTMLElement): Array<{ summary: HTMLButtonElement; text: () => string }> {
  return [...container.querySelectorAll<HTMLButtonElement>(".ui-disclosure-summary")].map((summary) => ({
    summary,
    text: () =>
      [...(summary.closest(".ui-disclosure")?.querySelectorAll(".cm-line") ?? [])]
        .map((line) => line.textContent ?? "")
        .join("\n"),
  }));
}

describe("GitHub push_files", () => {
  const files = [
    { path: "docs/test-notes.md", content: "# Test notes\n\nA paragraph." },
    { path: "src/test_main.go", content: 'package main\n\nfunc main() { println("test") }\n' },
    { path: "config/test.yaml", content: "key: value\n" },
    { path: "config/test-extra-1.yaml", content: "a: 1\n" },
    { path: "config/test-extra-2.yaml", content: "b: 2\n" },
  ];
  const args = { ...REPOSITORY, branch: "test-branch", message: "test: update notes", files };

  it("names the file count, branch and repository as given", async () => {
    const label = await mount(renderActionLabel(PUSH_FILES, args));
    expect(shownText(label)).toBe("Push 5 files to test-branch in test-owner/test-repo");
  });

  it("summarizes the commit message and the first files with their sizes", async () => {
    const collapsed = await mount(renderPaneCollapsed(PUSH_FILES, args));
    expect([...collapsed.querySelectorAll("p")].map((line) => line.textContent)).toEqual([
      "test: update notes",
      "docs/test-notes.md · 26 B",
      "src/test_main.go · 46 B",
      "config/test.yaml · 11 B",
      "and 2 more files",
    ]);
  });

  it("opens each file as its own block with its text, never escaped JSON", async () => {
    const opened = await mount(renderPaneOpened(PUSH_FILES, args));
    expect(opened.textContent).toContain("test: update notes");
    const blocks = fileBlocks(opened);
    expect(blocks.map(({ summary }) => summary.textContent)).toEqual(
      files.map((file) => expect.stringContaining(file.path))
    );
    expect(opened.textContent).not.toContain("package main");

    await act(async () => blocks[1].summary.click());
    expect(blocks[1].text()).toBe('package main\n\nfunc main() { println("test") }\n');
    expect(opened.textContent).not.toContain("\\n");
  });

  it("shows Markdown as prose and code in monospace", async () => {
    const opened = await mount(renderDetailsArguments(PUSH_FILES, args));
    const [markdown, code] = fileBlocks(opened);
    await act(async () => markdown.summary.click());
    await act(async () => code.summary.click());
    const fonts = [...opened.querySelectorAll(".ui-code-block-block")].map((block) =>
      block.classList.contains("ui-code-font-prose")
    );
    // The commit message, then the two opened files.
    expect(fonts).toEqual([true, true, false]);
  });

  it("offers card decisions for valid arguments and rejects unknown arguments", () => {
    expect(actionOffersCardDecisionButtons(PUSH_FILES, args)).toBe(true);
    expect(actionOffersCardDecisionButtons(PUSH_FILES, { ...args, force: true })).toBe(false);
  });
});

describe("GitHub delete_file", () => {
  const args = {
    ...REPOSITORY,
    path: "docs/test-notes.md",
    message: "docs: remove obsolete notes",
    branch: "test-branch",
  };

  it("shows the repository, path, branch, and commit message on the compact card", async () => {
    const label = await mount(renderActionLabel(DELETE_FILE, args));
    const collapsed = await mount(renderPaneCollapsed(DELETE_FILE, args));

    expect(shownText(label)).toBe("Delete docs/test-notes.md in test-owner/test-repo");
    expect(shownText(collapsed)).toContain("test-branch");
    expect(shownText(collapsed)).toContain("docs: remove obsolete notes");
    expect(actionCompactCardExpands(DELETE_FILE, args)).toBe(false);
    expect(actionOffersCardDecisionButtons(DELETE_FILE, args)).toBe(true);
  });

  it("keeps unknown arguments out of the decision-capable compact card", () => {
    expect(actionOffersCardDecisionButtons(DELETE_FILE, { ...args, sha: "unexpected" })).toBe(false);
    expect(actionCompactCardExpands(DELETE_FILE, { ...args, sha: "unexpected" })).toBe(true);
  });
});

describe("GitHub update_pull_request", () => {
  const base = { ...REPOSITORY, pullNumber: 7 };

  it.each([
    [{ state: "closed", body: "Closed pending test evidence." }, "Close test-owner/test-repo#7: replace description"],
    [{ state: "open" }, "Reopen test-owner/test-repo#7"],
    [{ title: "Test title", draft: true }, "Update test-owner/test-repo#7: retitle, mark as draft"],
    [{ body: "", draft: false }, "Update test-owner/test-repo#7: clear description, mark ready for review"],
    [
      { base: "test-base", maintainer_can_modify: false, reviewers: ["test-reviewer-1", "test-reviewer-2"] },
      "Update test-owner/test-repo#7: change base to test-base, disallow maintainer edits, request review from test-reviewer-1, test-reviewer-2",
    ],
  ])("names what %j changes", async (changes, expected) => {
    const label = await mount(renderActionLabel(UPDATE_PULL_REQUEST, { ...base, ...changes }));
    expect(shownText(label)).toBe(expected);
  });

  it("shows a new description as prose under its heading", async () => {
    const args = { ...base, title: "Test title", body: "Test description." };
    const collapsed = await mount(renderPaneCollapsed(UPDATE_PULL_REQUEST, args));
    const opened = await mount(renderPaneOpened(UPDATE_PULL_REQUEST, args));

    expect(collapsed.textContent).toContain("New title: Test title");
    expect(collapsed.textContent).toContain("Test description.");
    expect(opened.textContent).toContain("New title: Test title");
    expect(opened.querySelector("[data-operation-heading]")?.textContent).toBe("New description");
    expect(opened.querySelector(".ui-code-font-prose")?.textContent).toBe("Test description.");
  });

  it("leaves nothing below the label when it names every change", async () => {
    const collapsed = await mount(renderPaneCollapsed(UPDATE_PULL_REQUEST, { ...base, state: "closed" }));
    expect(shownText(collapsed)).toBe("");
  });

  it("offers card decisions for changes and rejects unknown or empty calls", () => {
    expect(actionOffersCardDecisionButtons(UPDATE_PULL_REQUEST, { ...base, state: "closed" })).toBe(true);
    expect(actionOffersCardDecisionButtons(UPDATE_PULL_REQUEST, { ...base, body: "Updated description." })).toBe(true);
    expect(actionOffersCardDecisionButtons(UPDATE_PULL_REQUEST, { ...base, invisible: "must review" })).toBe(false);
    expect(actionOffersCardDecisionButtons(UPDATE_PULL_REQUEST, base)).toBe(false);
  });
});

describe("GitHub create_branch", () => {
  const args = { ...REPOSITORY, branch: "test-branch", from_branch: "test-base" };

  it.each([
    [args, "Create branch test-branch from test-base in test-owner/test-repo"],
    [
      { ...REPOSITORY, branch: "test-branch" },
      "Create branch test-branch from the default branch in test-owner/test-repo",
    ],
  ])("puts every argument of %j in the label", async (callArgs, expected) => {
    const label = await mount(renderActionLabel(CREATE_BRANCH, callArgs));
    expect(shownText(label)).toBe(expected);
    expect(actionCompactCardExpands(CREATE_BRANCH, callArgs)).toBe(false);
  });

  it("offers card decision buttons only for arguments the label shows", async () => {
    expect(actionOffersCardDecisionButtons(CREATE_BRANCH, args)).toBe(true);
    const extra = { ...args, sha: "0000000" };
    expect(actionOffersCardDecisionButtons(CREATE_BRANCH, extra)).toBe(false);
    expect(shownText(await mount(renderActionLabel(CREATE_BRANCH, extra)))).toBe("Create GitHub branch");
    expect(actionCompactCardExpands(CREATE_BRANCH, extra)).toBe(true);
    expect((await mount(renderDetailsArguments(CREATE_BRANCH, extra))).textContent).toContain('"sha": "0000000"');
  });
});

describe("GitHub create_pull_request", () => {
  const args = {
    ...REPOSITORY,
    title: "Test title",
    head: "test-head",
    base: "test-base",
    draft: true,
    maintainer_can_modify: true,
  };

  it("puts the base and title in the label and the head and settings below it", async () => {
    const label = await mount(renderActionLabel(CREATE_PULL_REQUEST, args));
    const collapsed = await mount(renderPaneCollapsed(CREATE_PULL_REQUEST, args));

    expect(shownText(label)).toBe("Open PR into test-base of test-owner/test-repo: Test title");
    expect(collapsed.textContent).toContain("from test-head");
    expect([...collapsed.querySelectorAll(".mantine-Badge-label")].map((chip) => chip.textContent)).toEqual([
      "draft",
      "maintainers can edit",
    ]);
  });

  it("shows the description's first lines on the compact card and all of it when opened", async () => {
    const withBody = { ...args, body: "Test description." };
    for (const view of [renderPaneOpened, renderDetailsArguments]) {
      const container = await mount(view(CREATE_PULL_REQUEST, withBody));
      expect(container.querySelector("[data-operation-heading]")?.textContent).toBe("Description");
      expect(container.textContent).toContain("Test description.");
    }
    expect((await mount(renderPaneCollapsed(CREATE_PULL_REQUEST, withBody))).textContent).toContain(
      "Test description."
    );
    expect(actionOffersCardDecisionButtons(CREATE_PULL_REQUEST, withBody)).toBe(true);
  });
});

describe("GitHub search_pull_requests", () => {
  const args = {
    owner: "test-owner",
    repo: "test-repo",
    query: "is:open label:bug",
    page: 2,
    perPage: 10,
    sort: "updated",
    order: "desc",
    fields: ["number", "title", "state", "html_url"],
  };

  it("shows only query and scope on the compact card, with options in details", async () => {
    const label = await mount(renderActionLabel(SEARCH_PULL_REQUESTS, args));
    const collapsed = await mount(renderPaneCollapsed(SEARCH_PULL_REQUESTS, args));
    const opened = await mount(renderPaneOpened(SEARCH_PULL_REQUESTS, args));
    const details = await mount(renderDetailsArguments(SEARCH_PULL_REQUESTS, args));

    expect(shownText(label)).toBe("Search GitHub pull requests");
    expect(shownText(collapsed)).toContain("is:open label:bug");
    expect(shownText(collapsed)).toContain("Owner filter: test-owner");
    expect(shownText(collapsed)).toContain("Repository filter: test-repo");
    for (const compact of [shownText(collapsed), shownText(opened)]) {
      expect(compact).not.toContain("Page 2");
      expect(compact).not.toContain("results per page");
      expect(compact).not.toContain("Sort by");
      expect(compact).not.toContain("Order:");
      expect(compact).not.toContain("Fields:");
    }
    expect(shownText(details)).toContain("Page 2");
    expect(shownText(details)).toContain("10 results per page");
    expect(shownText(details)).toContain("Sort by updated");
    expect(shownText(details)).toContain("Order: desc");
    expect(shownText(details)).toContain("Fields: number, title, state, html_url");
    expect(actionCompactCardExpands(SEARCH_PULL_REQUESTS, args)).toBe(false);
    expect(actionOffersCardDecisionButtons(SEARCH_PULL_REQUESTS, args)).toBe(true);
  });

  it("rejects unknown or invalid search arguments from compact-card decisions", () => {
    expect(actionOffersCardDecisionButtons(SEARCH_PULL_REQUESTS, { ...args, repositoryId: 123 })).toBe(false);
    expect(actionOffersCardDecisionButtons(SEARCH_PULL_REQUESTS, { ...args, page: 0 })).toBe(false);
    expect(actionOffersCardDecisionButtons(SEARCH_PULL_REQUESTS, { ...args, perPage: 101 })).toBe(false);
    expect(actionCompactCardExpands(SEARCH_PULL_REQUESTS, { ...args, repositoryId: 123 })).toBe(true);
  });
});

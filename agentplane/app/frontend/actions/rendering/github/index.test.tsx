// @vitest-environment happy-dom
import { act } from "react";
import { create } from "@bufbuild/protobuf";

import { ActionIdentitySchema } from "../../../../../action_service/api/contracts/action_service_pb";
import { describe, expect, it } from "vitest";

import { mount } from "../../testing/harness";
import {
  actionCompactCardExpands,
  actionOffersCardDecisionButtons,
  renderActionLabel,
  renderActionOutcome,
  renderDetailsArguments,
  renderDetailsResult,
  renderPaneCollapsed,
  renderPaneOpened,
} from "../index";

const PUSH_FILES = create(ActionIdentitySchema, { group: "github", name: "push_files" });
const DELETE_FILE = create(ActionIdentitySchema, { group: "github", name: "delete_file" });
const UPDATE_PULL_REQUEST = create(ActionIdentitySchema, { group: "github", name: "update_pull_request" });
const CREATE_BRANCH = create(ActionIdentitySchema, { group: "github", name: "create_branch" });
const CREATE_PULL_REQUEST = create(ActionIdentitySchema, { group: "github", name: "create_pull_request" });
const SEARCH_PULL_REQUESTS = create(ActionIdentitySchema, { group: "github", name: "search_pull_requests" });

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

describe("GitHub issue label updates", () => {
  const action = create(ActionIdentitySchema, { group: "github", name: "issue_write" });
  const args = { ...REPOSITORY, method: "update", issue_number: 42, labels: ["bug", "upstream-waiting"] };

  it("shows the replacement label set and permits a decision on the closed card", async () => {
    expect(shownText(await mount(renderActionLabel(action, args)))).toBe(
      'Set labels on test-owner/test-repo#42: "bug", "upstream-waiting"'
    );
    expect(shownText(await mount(renderPaneCollapsed(action, args)))).toBe("");
    expect(actionOffersCardDecisionButtons(action, args)).toBe(true);
    expect(actionCompactCardExpands(action, args)).toBe(false);
    const clear = { ...args, labels: [] };
    expect(shownText(await mount(renderActionLabel(action, clear)))).toBe("Clear labels on test-owner/test-repo#42");
    expect(actionOffersCardDecisionButtons(action, clear)).toBe(true);
  });

  it.each([
    { ...args, title: "New title" },
    { ...args, state: "closed" },
    { ...args, future_field: true },
    { ...args, method: "create" },
    { ...args, issue_number: 0 },
    { ...args, labels: [""] },
    { ...args, labels: undefined },
  ])("requires full review for other or invalid arguments: %j", (input) => {
    expect(actionOffersCardDecisionButtons(action, input)).toBe(false);
    expect(actionCompactCardExpands(action, input)).toBe(true);
  });
});

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

  it("shows the search scope and result cap on both review surfaces", async () => {
    const label = await mount(renderActionLabel(SEARCH_PULL_REQUESTS, args));
    const collapsed = await mount(renderPaneCollapsed(SEARCH_PULL_REQUESTS, args));
    const details = await mount(renderDetailsArguments(SEARCH_PULL_REQUESTS, args));

    expect(shownText(label)).toBe("Search GitHub pull requests");
    for (const view of [collapsed, details]) {
      expect(shownText(view)).toContain("is:open label:bug");
      expect(shownText(view)).toContain("Repository test-owner/test-repo");
      expect(shownText(view)).not.toContain("results per page");
      expect(shownText(view)).not.toContain("Sort by");
      expect(shownText(view)).not.toContain("Fields:");
    }
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

describe("GitHub Action receipts", () => {
  it("links the returned branch and commit using the recorded repository", async () => {
    const commit = "0123456789abcdef0123456789abcdef01234567";
    const result = {
      content: [],
      structuredContent: { ref: "refs/heads/docs/test-branch", object: { type: "commit", sha: commit } },
      isError: false,
    };
    const receipt = await mount(
      renderDetailsResult(CREATE_BRANCH, result, {
        requestId: "test-request",
        arguments: { ...REPOSITORY, branch: "docs/test-branch" },
      })
    );
    const links = [...receipt.querySelectorAll("a")];
    expect(links.map((link) => link.getAttribute("href"))).toEqual([
      "https://github.com/test-owner/test-repo/tree/docs/test-branch",
      `https://github.com/test-owner/test-repo/commit/${commit}`,
    ]);
  });

  it.each([
    { content: [{ type: "text" as const, text: "Pull request branch update is in progress" }], isError: false },
    {
      content: [{ type: "text" as const, text: '{"message":"Updating pull request branch."}' }],
      structuredContent: { message: "Updating pull request branch." },
      isError: false,
    },
  ])("shows an accepted branch update as queued", async (result) => {
    const action = create(ActionIdentitySchema, { group: "github", name: "update_pull_request_branch" });
    expect(renderActionOutcome(action, result)).toEqual({ label: "Update queued", failed: false });
    const receipt = await mount(renderDetailsResult(action, result));
    expect(shownText(receipt)).toContain("GitHub accepted the update");
  });

  it("links matching PR titles and shows when the result is incomplete", async () => {
    const result = {
      content: [],
      structuredContent: {
        total_count: 2,
        incomplete_results: true,
        items: [
          {
            title: "Review staging changes",
            state: "open",
            html_url: "https://github.com/test-owner/test-repo/pull/5",
          },
          { title: "Unsafe URL", html_url: "javascript:alert(1)" },
        ],
      },
      isError: false,
    };
    const receipt = await mount(renderDetailsResult(SEARCH_PULL_REQUESTS, result));
    expect(shownText(receipt)).toContain("incomplete search results");
    expect(shownText(receipt)).toContain("test-owner/test-repo · Open");
    expect([...receipt.querySelectorAll("a")].map((link) => link.textContent)).toEqual(["Review staging changes"]);
  });
});

describe("GitHub workflow outcomes", () => {
  it("shows async cancellation as requested despite GitHub's success text", async () => {
    const action = create(ActionIdentitySchema, { group: "github", name: "actions_run_trigger" });
    const result = {
      content: [],
      structuredContent: { message: "Workflow run has been cancelled", run_id: 81, status_code: 202 },
      isError: false,
    };
    expect(renderActionOutcome(action, result)?.label).toBe("Cancellation requested");
    const view = await mount(
      renderDetailsResult(action, result, {
        requestId: "test-request",
        arguments: { ...REPOSITORY, method: "cancel_workflow_run", run_id: 81 },
      })
    );
    expect(shownText(view)).toContain("Cancellation requested");
    expect(view.querySelector("a")?.getAttribute("href")).toBe(
      "https://github.com/test-owner/test-repo/actions/runs/81"
    );
  });

  it("keeps submitted scan text and opaque scanner responses behind Raw", async () => {
    const action = create(ActionIdentitySchema, { group: "github", name: "run_secret_scanning" });
    const marker = "fixture-only-sensitive-value";
    const view = await mount(renderDetailsArguments(action, { ...REPOSITORY, files: [marker] }));
    expect(shownText(view)).toContain("One block");
    expect(shownText(view)).not.toContain(marker);
    for (const isError of [true, false]) {
      expect(
        renderDetailsResult(action, {
          content: [{ type: "text", text: marker }],
          structuredContent: { message: "This may be a different provider variant." },
          isError,
        })
      ).toBeNull();
    }
  });
});

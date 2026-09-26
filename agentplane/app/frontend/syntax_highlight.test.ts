// @vitest-environment jsdom
// jsdom rather than the happy-dom this package's other specs run in: under happy-dom, DOMPurify strips
// the first node of the markup it sanitizes, so text ahead of the first token would go missing there
// and in no browser.
import { describe, expect, it } from "vitest";

import { highlight } from "./syntax_highlight";

function parsed(html: string): HTMLElement {
  const element = document.createElement("div");
  element.innerHTML = html;
  return element;
}

describe("highlight", () => {
  it("keeps every character of a shell command, text ahead of the first token included", () => {
    const command = 'systemctl --user restart test-backup.service && echo "restarted at $(date -Is)"';
    const element = parsed(highlight(command, "bash"));
    expect(element.textContent).toBe(command);
    expect(element.querySelector(".hljs-built_in")?.textContent).toBe("echo");
    expect(element.querySelector(".hljs-string .hljs-subst")?.textContent).toBe("$(date -Is)");
  });

  it("leaves no live markup from the text it highlights, only the text", () => {
    const command = 'echo "<img src=x onerror=alert(1)>" && cat /tmp/test-file';
    const element = parsed(highlight(command, "bash"));
    expect(element.querySelector("img, [onerror]")).toBeNull();
    expect(element.textContent).toBe(command);
  });
});

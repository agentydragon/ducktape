import { describe, expect, it } from "vitest";

import { oneLine, parseCommandCall, shellInvocation } from "./command_calls";

describe("shellInvocation", () => {
  it.each([
    ["single-quoted", "/bin/bash -lc 'printf hi'", "printf hi"],
    [
      "double-quoted, as Codex joins it",
      String.raw`/bin/bash -lc "echo \"a b\" 'c' \$HOME \\ x"`,
      String.raw`echo "a b" 'c' $HOME \ x`,
    ],
    ["without a path", "bash -c script.sh", "script.sh"],
    ["spanning lines", "sh -c 'a\nb'", "a\nb"],
    ["an empty script", "zsh -lc ''", ""],
  ])("takes the script of a shell run %s", (_, line, script) => {
    expect(shellInvocation(line)?.script).toBe(script);
  });

  it("keeps the shell and its flags, so the thread can say how the command ran", () => {
    expect(shellInvocation("/bin/bash -lc 'ls'")?.invocation).toBe("/bin/bash -lc");
  });

  it.each([
    ["another program", "python3 -c 'print(1)'"],
    ["a shell given arguments of its own", "bash -c 'echo $0' name"],
    ["a shell run on a file", "bash script.sh"],
    ["a quote that never closes", "bash -lc 'oops"],
    ["a double quote that never closes", 'bash -lc "oops'],
    ["a backslash with nothing to escape", "bash -lc oops\\"],
    ["a double-quoted backslash with nothing to escape", 'bash -lc "oops\\'],
    ["nothing", ""],
  ])("takes no line that is %s", (_, line) => {
    expect(shellInvocation(line)).toBeNull();
  });
});

describe("parseCommandCall", () => {
  it("draws Claude's Bash by what the model said it is for, with the command behind it", () => {
    const call = parseCommandCall(
      "Bash",
      JSON.stringify({ command: "docker ps -a", description: "List containers", timeout: 120000 })
    );
    expect(call).toEqual({
      label: "Bash",
      summary: "List containers",
      description: "List containers",
      command: "docker ps -a",
      notes: ["Timeout 120000 ms"],
    });
  });

  it("falls back to the command, on one line, where Claude gave no description", () => {
    const call = parseCommandCall("Bash", JSON.stringify({ command: "set -e\n  make   test\n", description: "" }));
    expect(call?.summary).toBe("set -e make test");
    expect(call?.description).toBeUndefined();
  });

  it("says what changes how a Bash call runs", () => {
    const call = parseCommandCall(
      "Bash",
      JSON.stringify({ command: "make", run_in_background: true, dangerouslyDisableSandbox: true })
    );
    expect(call?.notes).toEqual(["Runs in the background", "Sandbox disabled for this call"]);
  });

  it("draws Codex's command as the script it ran, and says where and by what", () => {
    const call = parseCommandCall(
      "commandExecution",
      JSON.stringify({
        command: String.raw`/bin/bash -lc "curl -sS \"https://test.example/\""`,
        cwd: "/test-workspace",
      })
    );
    expect(call).toEqual({
      label: "Shell",
      summary: 'curl -sS "https://test.example/"',
      command: 'curl -sS "https://test.example/"',
      notes: ["Run by /bin/bash -lc", "In /test-workspace"],
    });
  });

  it("draws a Codex command that is not a shell script as the line it is", () => {
    const call = parseCommandCall(
      "commandExecution",
      JSON.stringify({ command: "git status", cwd: "/test-workspace" })
    );
    expect(call?.command).toBe("git status");
    expect(call?.notes).toEqual(["In /test-workspace"]);
  });

  it("takes no call it could not show whole", () => {
    expect(parseCommandCall("Bash", JSON.stringify({ command: "ls", test_extra: true }))).toBeNull();
    expect(parseCommandCall("Bash", JSON.stringify({ description: "no command" }))).toBeNull();
    expect(parseCommandCall("commandExecution", JSON.stringify({ command: "ls" }))).toBeNull();
  });

  it("takes no other tool, and no arguments that are still arriving", () => {
    expect(parseCommandCall("Read", JSON.stringify({ command: "ls" }))).toBeNull();
    expect(parseCommandCall("Bash", '{"command": "ls -')).toBeNull();
  });
});

describe("oneLine", () => {
  it("joins lines and cuts a very long one", () => {
    expect(oneLine("a\n\n  b")).toBe("a b");
    expect(oneLine("x".repeat(1000))).toHaveLength(300);
  });
});

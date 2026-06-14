<script lang="ts">
  import "../styles/shared.css";
  import JsonDisclosure from "./JsonDisclosure.svelte";

  import type { ToolItem } from "../shared/types";

  export let item: ToolItem;

  // ToolExec renders Exec-kind content; narrow once for field access.
  $: exec = item.content.content_kind === "Exec" ? item.content : null;

  // Sandbox-exec tools carry an SBPL policy string in their untyped `args`.
  function sbplPolicy(args: unknown): unknown {
    return args && typeof args === "object" ? (args as Record<string, unknown>).policy : undefined;
  }

  function copyText(text: string) {
    if (!text) return;
    if (navigator.clipboard?.writeText) {
      navigator.clipboard.writeText(text).catch(() => {});
    } else {
      const ta = document.createElement("textarea");
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      try {
        document.execCommand("copy");
      } finally {
        document.body.removeChild(ta);
      }
    }
  }

  function copyExec() {
    const c = item.content;
    if (c.content_kind !== "Exec") return;
    const parts: string[] = [];
    if (c.cmd) parts.push(`$ ${c.cmd}`);
    if (c.stdout) parts.push(c.stdout);
    if (c.stderr) parts.push(c.stderr);
    copyText(parts.join("\n"));
  }
</script>

<div class="terminal">
  <div class="kind">
    {item.tool}
    {#if item.decision}<span class="term-approval">[{item.decision}]</span>{/if}
    <button class="copy" title="Copy output" on:click={copyExec}>Copy</button>
  </div>
  {#if typeof item.tool === "string" && item.tool.endsWith("__sandbox_exec")}
    <JsonDisclosure label="SBPL Policy" value={sbplPolicy(exec?.args)} persistKey={`sbpl:${item.id}`} />
    <JsonDisclosure label="Raw output (JSON)" value={item.content} persistKey={`execraw:${item.id}`} />
  {/if}
  <div class="terminal-body">
    {#if exec?.cmd}
      <pre class="term-line mono">$ {exec.cmd}</pre>
    {/if}
    {#if exec?.stdout}
      <pre class="term-stdout mono">{exec.stdout}</pre>
    {/if}
    {#if exec?.stderr}
      <pre class="term-stderr mono">{exec.stderr}</pre>
    {/if}
    {#if exec && exec.exit_code !== null && exec.exit_code !== undefined}
      <div class="term-exit">[exit {exec.exit_code}]</div>
    {/if}
    {#if exec?.is_error}
      <div class="term-error">[error]</div>
    {/if}
  </div>
</div>

<style>
  .terminal .terminal-body {
    background: #111;
    color: #eee;
    border-radius: 6px;
    padding: 0.5rem;
    max-height: 18rem;
    overflow: auto;
  }
  .terminal pre {
    margin: 0.25rem 0;
    white-space: pre-wrap;
    word-break: break-word;
    font-size: 0.85rem;
    line-height: 1.35;
  }
  .term-line {
    color: #9cdcfe;
  }
  .term-stdout {
    color: #d4d4d4;
  }
  .term-stderr {
    color: #f28b82;
  }
  .term-exit {
    color: #8ab4f8;
    font-size: 0.75rem;
    margin: 0.1rem 0 0.4rem;
  }
  .term-error {
    color: #ffb4ab;
    font-size: 0.75rem;
    margin: 0.2rem 0;
  }
  .term-approval {
    color: #ffd54f;
    font-size: 0.8rem;
    margin: 0.1rem 0;
  }
</style>

<script lang="ts">
  import "../styles/shared.css";
  import { z } from "zod";

  import JsonDisclosure from "./JsonDisclosure.svelte";

  import type { ToolItem } from "../shared/types";

  export let item: ToolItem;

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

  function copyJson() {
    try {
      copyText(JSON.stringify(displayResult ?? {}, null, 2));
    } catch {
      // Ignore JSON serialization errors
    }
  }

  // Prefer structured_content when present (FastMCP CallToolResult)
  const CallToolResultZ = z.object({ structured_content: z.unknown().optional() }).passthrough();
  const StructuredOutZ = z
    .object({
      error: z.string().optional(),
      ok: z.boolean().optional(),
      rationale: z.string().optional(),
    })
    .passthrough();

  let displayResult: unknown = null;
  $: {
    const c = item?.content;
    const res: unknown = c && (c as any).content_kind === "Json" ? (c as any).result : undefined;
    if (res && typeof res === "object") {
      const parsed = CallToolResultZ.safeParse(res);
      if (parsed.success && parsed.data.structured_content !== undefined) {
        displayResult = parsed.data.structured_content;
      } else {
        displayResult = res;
      }
    } else {
      displayResult = res;
    }
  }
  $: errorMessage = (() => {
    const v = displayResult;
    if (v && typeof v === "object") {
      const parsed = StructuredOutZ.safeParse(v);
      if (parsed.success && typeof parsed.data.error === "string") return parsed.data.error;
    }
    return null;
  })();

  // JSON rendering is delegated to JsonDisclosure component
</script>

<div class="tool-json">
  <div class="tool-header">
    <code>{item.tool}</code>
    {#if displayResult}
      <button class="copy" title="Copy JSON output" on:click={copyJson}>Copy</button>
    {/if}
  </div>
  {#if (item.content as any)?.args}
    <JsonDisclosure label="Arguments" value={(item.content as any).args} persistKey={`args:${item.id}`} />
  {/if}
  {#if typeof item.tool === "string" && item.tool.endsWith("__sandbox_exec") && (item.content as any)?.args?.policy}
    <JsonDisclosure label="SBPL Policy" value={(item.content as any).args.policy} persistKey={`sbpl:${item.id}`} />
  {/if}
  {#if displayResult}
    {#if errorMessage}
      <div class="term-error">{errorMessage}</div>
    {/if}
    <JsonDisclosure
      label={`Output${(item.content as any)?.is_error ? " [error]" : ""}`}
      value={displayResult}
      open
      persistKey={`out:${item.id}`}
    />
    <JsonDisclosure label="Raw tool result" value={(item.content as any)?.result} persistKey={`rawres:${item.id}`} />
  {/if}
</div>

<style>
  .tool-json .tool-header {
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }
  .term-error {
    color: #c62828;
    font-size: 0.8rem;
    margin: 0.2rem 0;
    font-weight: 600;
  }
</style>

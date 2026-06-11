<script lang="ts">
  import { SvelteMap } from "svelte/reactivity";
  import { z } from "zod";

  import JsonDisclosure from "./JsonDisclosure.svelte";
  import { LEAN_BROWSER_LABELS, TOOL_RESOURCES_LIST, TOOL_RESOURCES_READ } from "../lib/collapsedTools";

  import type { ToolItem } from "../shared/types";

  export let items: ToolItem[] = [];

  // Persist expansion by group anchor (first item id) across UI updates
  const EXPANDED_BY_ANCHOR: SvelteMap<string, string | null> =
    (globalThis as any).__adgn_ctg_expanded || new SvelteMap();
  (globalThis as any).__adgn_ctg_expanded = EXPANDED_BY_ANCHOR;
  // index of expanded item within items, or null
  let expanded: number | null = null;
  $: {
    const anchor = items && items.length ? items[0].id : null;
    if (anchor) {
      const openId = EXPANDED_BY_ANCHOR.get(anchor) || null;
      if (openId) {
        const idx = items.findIndex((it) => it.id === openId);
        expanded = idx >= 0 ? idx : null;
      }
    }
  }
  function setExpanded(next: number | null) {
    expanded = next;
    const anchor = items && items.length ? items[0].id : null;
    if (anchor) EXPANDED_BY_ANCHOR.set(anchor, next === null ? null : items[next].id);
  }

  function isJsonContent(x: any): boolean {
    return !!x && typeof x === "object" && x.content_kind === "Json";
  }

  // If all items are the same tool, we can render compact icon-only tokens
  $: allSameTool = items && items.length > 1 && items.every((it) => it.tool === items[0]?.tool);

  function collapsedLabelFor(it: ToolItem): string {
    const name = it.tool || "";
    const args: any = isJsonContent(it.content) ? (it.content as any).args : null;
    const leanLabel = LEAN_BROWSER_LABELS[name];
    if (leanLabel) {
      return leanLabel;
    }
    if (name === TOOL_RESOURCES_LIST) {
      const s = args?.server ? `server=${args.server}` : "server=*";
      const p = args?.uri_prefix ? `, prefix=${args.uri_prefix}` : "";
      return `List Resources(${s}${p})`;
    }
    if (name === TOOL_RESOURCES_READ) {
      const s = args?.server ? `server=${args.server}` : "";
      const u = args?.uri ? `uri=${args.uri}` : "";
      const parts = [u, s].filter(Boolean).join(", ");
      return `Read Resource(${parts})`;
    }
    // Fallback: show the raw tool key
    return it.tool;
  }

  // Determine success/error state for compact tokens
  function isError(it: ToolItem): boolean | null {
    const c: any = it.content;
    if (!c || typeof c !== "object") return null;
    if (c.content_kind === "Exec") {
      if (typeof c.is_error === "boolean") return c.is_error;
      if (typeof c.exit_code === "number") return c.exit_code !== 0;
      return null;
    }
    if (c.content_kind === "Json") {
      if (typeof c.is_error === "boolean") return c.is_error;
      return null;
    }
    return null;
  }

  // For JSON content, display structured_content when present, else raw result
  const CallToolResultZ = z.object({ structured_content: z.unknown().optional() }).passthrough();
  function jsonOutput(it: ToolItem): unknown {
    const c: any = it?.content;
    if (!c || c.content_kind !== "Json") return null;
    const res: any = c.result;
    if (res && typeof res === "object") {
      const parsed = CallToolResultZ.safeParse(res);
      if (parsed.success && parsed.data.structured_content !== undefined) {
        return parsed.data.structured_content;
      }
    }
    return res ?? null;
  }
</script>

<div class="collapsed-tools-group">
  <div class="inline-list">
    {#if allSameTool}
      {@const label = collapsedLabelFor(items[0])}
      {#each items as it, idx (it.id)}
        {@const err = isError(it)}
        <button
          type="button"
          class="icon-token {expanded === idx ? 'active' : ''}"
          on:click={() => setExpanded(expanded === idx ? null : idx)}
          aria-label={`View ${label} #${idx + 1}`}
          title={label}
        >
          {#if err === true}
            <span class="status err" aria-label="error">✗</span>
          {:else if err === false}
            <span class="status ok" aria-label="ok">✓</span>
          {:else}
            <span class="status ok" aria-label="ok">✓</span>
          {/if}
        </button>
      {/each}
      <span class="group-label">{label}</span>
    {:else}
      {#each items as it, idx (it.id)}
        {@const err = isError(it)}
        <button
          type="button"
          class="token {expanded === idx ? 'active' : ''}"
          on:click={() => setExpanded(expanded === idx ? null : idx)}
        >
          {#if err === true}
            <span class="status err" aria-label="error" title="error">✗</span>
          {:else if err === false}
            <span class="status ok" aria-label="ok" title="ok">✓</span>
          {/if}
          {collapsedLabelFor(it)}
        </button>{#if idx < items.length - 1},
        {/if}
      {/each}
    {/if}
  </div>

  {#if expanded !== null}
    {@const it = items[expanded]}
    <div class="expanded">
      <JsonDisclosure
        label="Input"
        value={isJsonContent(it.content) ? (it.content as any).args : null}
        persistKey={`ctg:in:${it.id}`}
      />
      <JsonDisclosure label="Output" value={jsonOutput(it)} persistKey={`ctg:out:${it.id}`} />
    </div>
  {/if}
</div>

<style>
  .collapsed-tools-group {
    margin: 0.25rem 0;
  }
  .inline-list {
    font-size: 0.85rem;
    color: var(--text);
  }
  .token {
    background: none;
    border: none;
    padding: 0;
    color: var(--accent);
    cursor: pointer;
    font: inherit;
    text-decoration: underline;
  }
  .token.active {
    text-decoration: none;
    font-weight: 600;
  }
  .icon-token {
    background: none;
    border: none;
    padding: 0 0.125rem;
    cursor: pointer;
    font: inherit;
    text-decoration: none;
  }
  .icon-token.active {
    filter: brightness(0.9);
  }
  .expanded {
    margin-top: 0.25rem;
    padding-left: 0.5rem;
    border-left: 2px solid var(--border);
  }
  /* Removed unused .row */
  .status {
    display: inline;
    text-decoration: none;
  }
  .status.ok {
    color: #2e7d32;
  }
  .status.err {
    color: #c62828;
  }
  .group-label {
    margin-left: 0.25rem;
    color: var(--text);
  }
</style>

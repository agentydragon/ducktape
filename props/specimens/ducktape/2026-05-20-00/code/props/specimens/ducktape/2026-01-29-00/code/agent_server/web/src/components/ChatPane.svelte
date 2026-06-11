<script lang="ts">
  import "../styles/shared.css";
  import DOMPurify from "dompurify";
  // @ts-ignore – asset URL import handled by Vite
  import darkThemeUrl from "highlight.js/styles/github-dark.css?url";
  // @ts-ignore – asset URL import handled by Vite
  import lightThemeUrl from "highlight.js/styles/github.css?url";
  import { onMount } from "svelte";

  import CollapsedToolsGroup from "./CollapsedToolsGroup.svelte";
  import ToolExec from "./ToolExec.svelte";
  import ToolJson from "./ToolJson.svelte";
  import { currentAgentId } from "../features/agents/stores";
  import {
    uiState as uiStateStore,
    lastError as lastErrorStore,
    agentPhase as agentPhaseStore,
    sendPrompt,
    abortAgent,
  } from "../features/chat/stores";
  import { isCollapsedToolKey } from "../lib/collapsedTools";
  import { renderMarkdown as renderMarkdownHtml } from "../shared/markdown";

  import type { UiDisplayItem, ToolItem as TToolItem } from "../shared/types";

  export let renderMarkdown: boolean = true;

  let prompt = "";
  let messagesEl: HTMLDivElement | null = null;
  let promptEl: HTMLTextAreaElement | null = null;
  $: agentPhase = $agentPhaseStore;
  // Consider agent busy for these transient states; allow send only when idle/finished
  $: busy =
    agentPhase === "running" ||
    agentPhase === "awaiting_approval" ||
    agentPhase === "starting" ||
    agentPhase === "aborting";

  function sendPromptLocal() {
    if (busy || !prompt.trim()) return;
    sendPrompt(prompt);
    // Clear the prompt immediately on submit and persist cleared draft
    const id = $currentAgentId;
    prompt = "";
    if (id) localStorage.setItem(`composer:${id}`, "");
  }
  function onPromptKeydown(e: KeyboardEvent) {
    // Send on Enter (no Shift), also accept Cmd/Ctrl+Enter. Preserve IME composing.
    const isEnter = e.key === "Enter";
    const allowSend = isEnter && !e.shiftKey && !e.altKey && !e.isComposing;
    const modSend = isEnter && (e.metaKey || e.ctrlKey) && !e.shiftKey && !e.isComposing;
    if ((allowSend || modSend) && !busy && prompt.trim()) {
      e.preventDefault();
      e.stopPropagation();
      sendPromptLocal();
    }
  }
  function onPromptInput() {
    const id = $currentAgentId;
    if (id) localStorage.setItem(`composer:${id}`, prompt);
  }

  // Do not auto-clear prompt on finish; allow composing while a run is in progress
  function copyText(text: string) {
    if (!text) return;
    if (navigator.clipboard && navigator.clipboard.writeText) {
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

  onMount(() => {
    requestAnimationFrame(() => {
      promptEl?.focus();
    });
  });

  // Per-agent draft persistence
  let lastAgentId: string | null = null;
  $: if ($currentAgentId !== lastAgentId) {
    // Save previous agent's draft
    if (lastAgentId) localStorage.setItem(`composer:${lastAgentId}`, prompt);
    // Load new agent's draft
    const id = $currentAgentId;
    if (id) {
      const saved = localStorage.getItem(`composer:${id}`);
      prompt = saved ?? "";
    } else {
      prompt = "";
    }
    lastAgentId = id ?? null;
  }

  // No DOM scanning needed; Marked emits highlighted HTML with 'hljs' class

  // Load highlight.js themes via media attributes (no JS toggling needed)
  onMount(() => {
    const ensure = (id: string, href: string, media: string) => {
      if (!document.getElementById(id)) {
        const link = document.createElement("link");
        link.rel = "stylesheet";
        link.id = id;
        link.href = href;
        link.media = media;
        document.head.appendChild(link);
      }
    };
    ensure("hljs-theme-light", lightThemeUrl, "(prefers-color-scheme: light)");
    ensure("hljs-theme-dark", darkThemeUrl, "(prefers-color-scheme: dark)");
  });

  // Collapsing logic: group consecutive tool items from a configured set
  type RenderBlock = { kind: "group"; items: TToolItem[] } | { kind: "item"; item: UiDisplayItem };

  function isCollapsedTool(it: UiDisplayItem): it is TToolItem {
    return it?.kind === "Tool" && isCollapsedToolKey((it as TToolItem).tool);
  }

  function renderItems(items: UiDisplayItem[]): RenderBlock[] {
    const out: RenderBlock[] = [];
    for (let i = 0; i < items.length; i++) {
      const it = items[i];
      if (isCollapsedTool(it)) {
        const group: TToolItem[] = [it as TToolItem];
        let j = i + 1;
        while (j < items.length && isCollapsedTool(items[j] as UiDisplayItem)) {
          group.push(items[j] as TToolItem);
          j++;
        }
        out.push({ kind: "group", items: group });
        i = j - 1;
        continue;
      }
      out.push({ kind: "item", item: it });
    }
    return out;
  }

  // Precompute blocks for rendering with lookahead
  $: blocks = renderItems($uiStateStore && $uiStateStore.items ? $uiStateStore.items : []);

  // Key function for each blocks - use item ID or first item's ID for groups
  function blockKey(block: RenderBlock): string {
    if (block.kind === "item") {
      return block.item.id;
    } else {
      return block.items[0]?.id || "empty-group";
    }
  }
</script>

<section class="chat" {...$$restProps}>
  {#if $lastErrorStore}
    <div class="error">Error: {$lastErrorStore}</div>
  {/if}

  <div class="messages" bind:this={messagesEl}>
    {#if !$uiStateStore || !($uiStateStore.items && $uiStateStore.items.length)}
      <div class="empty">No messages yet.</div>
    {:else}
      {#key $uiStateStore.seq}
        {#each blocks as block, i (blockKey(block))}
          {@const nextBlock = blocks[i + 1]}
          {@const isNextEndTurn = nextBlock?.kind === "item" && nextBlock.item.kind === "EndTurn"}
          <div
            class="msg"
            class:endturn={block.kind === "item" && block.item.kind === "EndTurn"}
            class:no-border={isNextEndTurn}
          >
            {#if block.kind === "group"}
              <CollapsedToolsGroup items={block.items} />
            {:else if block.kind === "item" && block.item.kind === "UserMessage"}
              <div class="header">
                <div class="kind">UserMessage</div>
              </div>
              <div class="text">{block.item.text}</div>
            {:else if block.kind === "item" && block.item.kind === "AssistantMarkdown"}
              {@const markdownItem = block.item}
              <div class="header">
                <div class="kind">AssistantMarkdown</div>
                <button class="copy" title="Copy text" on:click={() => copyText(markdownItem.md || "")}>Copy</button>
              </div>
              {#if renderMarkdown}
                <div class="text md">
                  <!-- eslint-disable-next-line svelte/no-at-html-tags -->
                  {@html DOMPurify.sanitize(renderMarkdownHtml(markdownItem.md || ""))}
                </div>
              {:else}
                <div class="text">{markdownItem.md}</div>
              {/if}
            {:else if block.kind === "item" && block.item.kind === "EndTurn"}
              <div class="end-turn-separator"></div>
            {:else if block.kind === "item" && block.item.kind === "Tool"}
              {#if block.item.content?.content_kind === "Exec"}
                <ToolExec item={block.item} />
              {:else if block.item.content?.content_kind === "Json"}
                <ToolJson item={block.item} />
              {/if}
            {/if}
          </div>
        {/each}
      {/key}
    {/if}
  </div>

  <form class="composer" on:submit|preventDefault={sendPromptLocal}>
    <textarea
      bind:this={promptEl}
      bind:value={prompt}
      rows="3"
      placeholder={busy
        ? "Agent is working… (Shift+Enter for newline)"
        : "Type a prompt… (Enter to send, Shift+Enter for newline)"}
      on:input={onPromptInput}
      on:keydown={onPromptKeydown}
    ></textarea>
    {#if $agentPhaseStore === "running" || $agentPhaseStore === "starting"}
      <button type="button" class="abort" title="Abort agent" on:click={() => abortAgent()}>Abort</button>
    {/if}
    <button type="submit" disabled={busy || !prompt.trim()} aria-disabled={busy || !prompt.trim()}>Send</button>
  </form>
</section>

<style>
  /* Column flex: scrollable messages + fixed composer */
  .chat {
    display: flex;
    flex-direction: column;
    height: 100%;
    min-height: 0;
    overflow: hidden;
    min-width: 0;
  }
  .messages {
    flex: 1 1 auto;
    min-height: 0;
    height: auto;
    overflow-y: auto;
    -webkit-overflow-scrolling: touch;
    scrollbar-gutter: stable both-edges;
    padding: 0.5rem;
    display: block;
    font-size: 0.92rem;
    line-height: 1.3;
  }
  .messages > .msg {
    margin-bottom: 0.25rem;
  }
  .msg {
    border-bottom: 1px solid var(--border);
    padding-bottom: 0.25rem;
  }
  .msg.no-border {
    border-bottom: none;
  }
  .msg.endturn {
    border-bottom: none;
    padding-bottom: 0;
    margin-bottom: 0;
  }
  .header {
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }
  .kind {
    font-size: 0.75rem;
    color: var(--muted);
  }
  .text {
    white-space: pre-wrap;
  }
  .text.md {
    white-space: normal;
  }
  /* Ensure code blocks don’t wrap and can scroll horizontally */
  .text.md :global(pre) {
    overflow-x: auto;
    margin: 0.25rem 0;
  }
  .text.md :global(code) {
    white-space: pre;
  }
  /* Reduce inter-paragraph spacing inside rendered markdown */
  .text.md :global(p) {
    margin: 0.2rem 0;
  }
  .text.md :global(p:first-child) {
    margin-top: 0;
  }
  .text.md :global(p:last-child) {
    margin-bottom: 0;
  }
  .end-turn-separator {
    height: 4px;
    background: #666;
    border: none;
    margin: 0;
    border-radius: 2px;
  }
  /* Keep composer always visible; prevent shrinking; sticky as a guard */
  .composer {
    display: flex;
    gap: 0.5rem;
    padding: 0.5rem;
    border-top: 1px solid var(--border);
    flex: 0 0 auto;
    position: sticky;
    bottom: 0;
    background: var(--surface);
  }
  .composer textarea {
    flex: 1 1 auto;
    resize: vertical;
    min-height: 2rem;
    display: block;
    width: 100%;
  }
  .composer button {
    white-space: nowrap;
  }
  .composer .abort {
    background: #b00020;
    color: #fff;
    border-color: #b00020;
  }
  .composer .abort:hover {
    filter: brightness(0.95);
  }
</style>

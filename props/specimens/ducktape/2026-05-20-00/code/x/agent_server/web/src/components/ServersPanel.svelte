<script lang="ts">
  import "../styles/shared.css";
  // @ts-ignore - library ships no types
  import JSONFormatter from "json-formatter-js";
  import { SvelteMap } from "svelte/reactivity";

  import ModalBackdrop from "./ModalBackdrop.svelte";
  import { attachMcpServer, detachMcpServer, refreshSnapshot } from "../features/chat/stores";
  import { MCP_PRESETS } from "../features/mcp/presets";
  import { buildSpecFromForm } from "../features/mcp/schema";
  import { currentAgentId } from "../shared/router";

  import type { ServerEntry } from "../shared/types";

  export let servers: ServerEntry[] = [];

  // Info modal state
  let showInfoModal = false;
  let infoServer: any | null = null;
  function openInfo(health: any) {
    infoServer = health;
    showInfoModal = true;
  }
  let modalToolExpanded = new SvelteMap<string, boolean>();
  function toggleModalTool(key: string) {
    modalToolExpanded.set(key, !modalToolExpanded.get(key));
    modalToolExpanded = modalToolExpanded;
  }

  // Collapsible JSON view action
  function jsonView(node: HTMLElement, value: any) {
    const prefersDark =
      typeof window !== "undefined" && window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
    const render = (val: any) => {
      node.innerHTML = "";
      let parsed: any = null;
      if (val && typeof val === "object") parsed = val;
      else if (typeof val === "string") {
        try {
          parsed = JSON.parse(val);
        } catch {
          parsed = null;
        }
      }
      if (parsed && typeof parsed === "object") {
        const fmt = new (JSONFormatter as any)(parsed, 1, {
          theme: prefersDark ? "dark" : undefined,
          hoverPreviewEnabled: true,
        });
        node.appendChild(fmt.render());
      } else {
        const pre = document.createElement("pre");
        pre.className = "pre";
        pre.textContent = typeof val === "string" ? val : String(val);
        node.appendChild(pre);
      }
    };
    render(value);
    return { update: (v: any) => render(v) };
  }

  // Manage servers state/logic (moved into the top script)
  let preset: string = "";
  let newName = "";
  let transport: "stdio" | "sse" | "inproc" | "http" = "stdio";

  // stdio fields
  let stdioCommand = "";
  let stdioArgs = "[]";
  let stdioEnv = "{}";

  // sse fields
  let sseUrl = "";
  let sseHeaders = "{}";
  let sseTimeout: number | string = 5;
  let sseReadTimeout: number | string = 300;

  // inproc fields
  let inprocFactory = "";
  let inprocArgs = "[]";
  let inprocKwargs = "{}";
  // http fields
  let httpUrl = "";
  let httpHeaders = "{}";
  let httpAuth = "";
  let httpTimeout: number | string = 30;
  let httpReadTimeout: number | string = 300;

  // async action state
  let attaching = false;
  let attachMsg: string | null = null;
  let attachErr: string | null = null;

  // per-field validation errors from zod builder + attach gating
  let fieldErrors: Record<string, string[]> = {};
  let topErrors: string[] = [];
  $: attachEnabled = !attaching && !!newName.trim() && !!previewSpec && Object.keys(fieldErrors).length === 0;

  // reactive preview values
  let previewSpec: any = null;
  let previewJsonStr: string = "{}";

  // Build preview + collect errors using zod-based schema
  $: {
    const res = buildSpecFromForm({
      transport,
      stdioCommand,
      stdioArgs,
      stdioEnv,
      sseUrl,
      sseHeaders,
      sseTimeout,
      sseReadTimeout,
      httpUrl,
      httpHeaders,
      httpAuth,
      httpTimeout,
      httpReadTimeout,
      inprocFactory,
      inprocArgs,
      inprocKwargs,
    });
    previewSpec = res.spec ?? null;
    fieldErrors = res.fieldErrors || {};
    topErrors = res.errors || [];
    previewJsonStr = (() => {
      try {
        return JSON.stringify(previewSpec ?? {}, null, 2);
      } catch {
        return "{}";
      }
    })();
  }

  function applyPresetFrom(id: string) {
    const p = MCP_PRESETS.find((it) => it.id === id);
    if (!p) return;
    transport = p.transport;
    if (p.defaultName && !newName) newName = p.defaultName;
    if (p.transport === "stdio" && p.defaults.stdio) {
      stdioCommand = p.defaults.stdio.command;
      stdioArgs = JSON.stringify(p.defaults.stdio.args ?? [], null, 2);
      stdioEnv = JSON.stringify(p.defaults.stdio.env ?? {}, null, 2);
    } else if (p.transport === "inproc" && p.defaults.inproc) {
      inprocFactory = p.defaults.inproc.factory;
      inprocArgs = JSON.stringify(p.defaults.inproc.args ?? [], null, 2);
      inprocKwargs = JSON.stringify(p.defaults.inproc.kwargs ?? {}, null, 2);
    } else if (p.transport === "sse" && p.defaults.sse) {
      sseUrl = p.defaults.sse.url;
      sseHeaders = JSON.stringify(p.defaults.sse.headers ?? {}, null, 2);
      sseTimeout = p.defaults.sse.timeout_secs;
      sseReadTimeout = p.defaults.sse.sse_read_timeout_secs;
    } else if (p.transport === "http" && p.defaults.http) {
      httpUrl = p.defaults.http.url;
      httpHeaders = JSON.stringify(p.defaults.http.headers ?? {}, null, 2);
      httpAuth = p.defaults.http.auth ?? "";
    }
  }

  // Remove legacy local JSON validators; rely on zod builder above

  async function onAttach() {
    if (!$currentAgentId) {
      alert("No agent selected");
      return;
    }
    const { spec, fieldErrors: fe } = buildSpecFromForm({
      transport,
      stdioCommand,
      stdioArgs,
      stdioEnv,
      sseUrl,
      sseHeaders,
      sseTimeout,
      sseReadTimeout,
      httpUrl,
      httpHeaders,
      httpAuth,
      httpTimeout,
      httpReadTimeout,
      inprocFactory,
      inprocArgs,
      inprocKwargs,
    });
    if (!newName.trim()) {
      attachErr = "Server name required";
      return;
    }
    if (!spec || (fe && Object.keys(fe).length)) {
      attachErr = "Please fix highlighted errors";
      return;
    }
    attaching = true;
    attachErr = null;
    attachMsg = "Attaching…";
    try {
      await attachMcpServer(newName, spec);
      attachMsg = "Attached. Refreshing…";
      await new Promise((r) => setTimeout(r, 150));
      refreshSnapshot();
      attachMsg = "Attached.";
    } catch (e: any) {
      attachErr = e?.message || String(e);
    } finally {
      attaching = false;
      setTimeout(() => {
        attachMsg = null;
        attachErr = null;
      }, 2000);
    }
  }

  async function onDetach(name: string) {
    if (!$currentAgentId) {
      alert("No agent selected");
      return;
    }
    try {
      attaching = true;
      attachErr = null;
      attachMsg = `Detaching ${name}…`;
      await detachMcpServer(name);
      attachMsg = "Detached. Refreshing…";
      await new Promise((r) => setTimeout(r, 150));
      refreshSnapshot();
      attachMsg = "Detached.";
    } catch (e: any) {
      attachErr = e?.message || String(e);
    } finally {
      attaching = false;
      setTimeout(() => {
        attachMsg = null;
        attachErr = null;
      }, 2000);
    }
  }

  // --- Visible server meta helpers ---
  function instSnippet(text: string | null | undefined): string | null {
    if (!text || typeof text !== "string") return null;
    const line = text.split(/\r?\n/)[0] || "";
    return line.length > 160 ? line.slice(0, 157) + "…" : line;
  }
</script>

<div class="servers">
  <details class="manage">
    <summary>Manage MCP Servers</summary>
    <div class="manage-body">
      <div class="row">
        <div class="col">
          <div class="field">
            <label class="inline" for="preset-select">Preset</label>
            <select
              id="preset-select"
              bind:value={preset}
              on:change={(e) => applyPresetFrom((e.target as HTMLSelectElement).value)}
            >
              <option value="">Custom</option>
              {#each MCP_PRESETS as p (p.id)}
                <option value={p.id}>{p.label}</option>
              {/each}
            </select>
          </div>
        </div>
        <div class="col">
          <div class="field">
            <label class="inline" for="server-name">Server Name</label>
            <input id="server-name" type="text" placeholder="server name" bind:value={newName} />
            {#if !newName.trim()}<div class="err">name required</div>{/if}
          </div>
        </div>
      </div>

      <div class="row">
        <div class="col">
          <div class="field">
            <label class="inline" for="transport-select">Transport</label>
            <select id="transport-select" bind:value={transport}>
              <option value="stdio">stdio</option>
              <option value="sse">sse</option>
              <option value="inproc">inproc</option>
              <option value="http">http</option>
            </select>
          </div>
        </div>
      </div>

      {#if transport === "stdio"}
        <div class="row">
          <div class="col grow">
            <label>command <input type="text" placeholder="/path/to/binary" bind:value={stdioCommand} /></label>
            {#if fieldErrors.stdioCommand}<div class="err">
                {fieldErrors.stdioCommand.join(", ")}
              </div>{/if}
          </div>
        </div>
        <div class="row">
          <div class="col grow">
            <label>args (JSON array) <textarea rows="3" bind:value={stdioArgs} spellcheck={false}></textarea></label>
            {#if fieldErrors.stdioArgs}<div class="err">
                {fieldErrors.stdioArgs.join(", ")}
              </div>{/if}
          </div>
          <div class="col grow">
            <label>env (JSON object) <textarea rows="3" bind:value={stdioEnv} spellcheck={false}></textarea></label>
            {#if fieldErrors.stdioEnv}<div class="err">{fieldErrors.stdioEnv.join(", ")}</div>{/if}
          </div>
        </div>
      {:else if transport === "sse"}
        <div class="row">
          <div class="col grow">
            <label>url <input type="text" placeholder="http://127.0.0.1:8000/sse" bind:value={sseUrl} /></label>
            {#if fieldErrors.sseUrl}<div class="err">{fieldErrors.sseUrl.join(", ")}</div>{/if}
          </div>
        </div>
        <div class="row">
          <div class="col grow">
            <label
              >headers (JSON object) <textarea rows="3" bind:value={sseHeaders} spellcheck={false}></textarea></label
            >
            {#if fieldErrors.sseHeaders}<div class="err">
                {fieldErrors.sseHeaders.join(", ")}
              </div>{/if}
          </div>
          <div class="col">
            <label>timeout_secs (s) <input type="number" min="1" bind:value={sseTimeout} /></label>
            {#if fieldErrors.sseTimeout}<div class="err">
                {fieldErrors.sseTimeout.join(", ")}
              </div>{/if}
          </div>
          <div class="col">
            <label>sse_read_timeout_secs (s) <input type="number" min="1" bind:value={sseReadTimeout} /></label>
            {#if fieldErrors.sseReadTimeout}<div class="err">
                {fieldErrors.sseReadTimeout.join(", ")}
              </div>{/if}
          </div>
        </div>
      {:else if transport === "http"}
        <div class="row">
          <div class="col grow">
            <label>url <input type="text" placeholder="http://127.0.0.1:8768/mcp" bind:value={httpUrl} /></label>
            {#if fieldErrors.httpUrl}<div class="err">{fieldErrors.httpUrl.join(", ")}</div>{/if}
          </div>
        </div>
        <div class="row">
          <div class="col grow">
            <label
              >headers (JSON object) <textarea rows="3" bind:value={httpHeaders} spellcheck={false}></textarea></label
            >
            {#if fieldErrors.httpHeaders}<div class="err">
                {fieldErrors.httpHeaders.join(", ")}
              </div>{/if}
          </div>
          <div class="col">
            <label>auth (Bearer) <input type="text" bind:value={httpAuth} placeholder="optional" /></label>
            {#if fieldErrors.httpAuth}<div class="err">{fieldErrors.httpAuth.join(", ")}</div>{/if}
          </div>
        </div>
        <div class="row">
          <div class="col">
            <label>timeout_secs (s) <input type="number" min="1" bind:value={httpTimeout} /></label>
            {#if fieldErrors.httpTimeout}<div class="err">
                {fieldErrors.httpTimeout.join(", ")}
              </div>{/if}
          </div>
          <div class="col">
            <label>read_timeout_secs (s) <input type="number" min="1" bind:value={httpReadTimeout} /></label>
            {#if fieldErrors.httpReadTimeout}<div class="err">
                {fieldErrors.httpReadTimeout.join(", ")}
              </div>{/if}
          </div>
        </div>
      {:else}
        <div class="row">
          <div class="col grow">
            <label>factory <input type="text" placeholder="pkg.mod:make_server" bind:value={inprocFactory} /></label>
            {#if fieldErrors.inprocFactory}<div class="err">
                {fieldErrors.inprocFactory.join(", ")}
              </div>{/if}
          </div>
        </div>
        <div class="row">
          <div class="col grow">
            <label>args (JSON array) <textarea rows="3" bind:value={inprocArgs} spellcheck={false}></textarea></label>
            {#if fieldErrors.inprocArgs}<div class="err">
                {fieldErrors.inprocArgs.join(", ")}
              </div>{/if}
          </div>
          <div class="col grow">
            <label
              >kwargs (JSON object) <textarea rows="3" bind:value={inprocKwargs} spellcheck={false}></textarea></label
            >
            {#if fieldErrors.inprocKwargs}<div class="err">
                {fieldErrors.inprocKwargs.join(", ")}
              </div>{/if}
          </div>
        </div>
      {/if}

      <div class="row">
        <button class="small" type="button" on:click={() => onAttach()} disabled={!attachEnabled}>Attach Server</button>
        {#if attachMsg}<span class="note">{attachMsg}</span>{/if}
        {#if attachErr}<span class="err">{attachErr}</span>{/if}
        {#if topErrors.length}
          <span class="err" title={topErrors.join(", ")}>
            Note: {topErrors.join(", ")}
          </span>
        {/if}
      </div>

      <details class="preview">
        <summary>Preview JSON</summary>
        <pre class="pre">{previewJsonStr}</pre>
        {#if topErrors.length}
          <div class="err">{topErrors.join(", ")}</div>
        {/if}
      </details>
    </div>
  </details>
  <h4>MCP Servers</h4>
  {#if servers && servers.length}
    {#each servers as health (health.name)}
      {@const serverName = health.name}
      {@const serverTools = health.state === "running" ? (health.tools ?? []) : []}
      {@const errorMsg = health.state === "failed" ? health.error : null}
      {@const failureText = errorMsg ? `: ${errorMsg}` : ""}
      <div class="server-item">
        <div class="server-header-row">
          <div
            class="server-header"
            title={health?.state === "running" ? "running" : `failed${failureText}`}
            role="button"
            tabindex="0"
            on:click={() => openInfo(health)}
            on:keydown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                openInfo(health);
              }
            }}
          >
            <span
              class="dot {health?.state === 'running' ? 'on' : 'off'}"
              title={health?.state === "running" ? "running" : `failed${failureText}`}
            ></span>
            <span class="server-name">{serverName}</span>
            <span class="tool-count" title={`${serverTools.length} tools`}>🛠 {serverTools.length}</span>
          </div>
          <button
            class="small detach-btn"
            type="button"
            title="Detach server"
            aria-label="Detach server"
            on:click={() => onDetach(serverName)}
          >
            <svg
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
              stroke-linecap="round"
              stroke-linejoin="round"
              aria-hidden="true"
            >
              <circle cx="12" cy="12" r="10"></circle>
              <line x1="9" y1="9" x2="15" y2="15"></line>
              <line x1="15" y1="9" x2="9" y2="15"></line>
            </svg>
            <span class="label">Detach</span>
          </button>
        </div>
        <!-- Visible server meta (instructions snippet + capabilities badges) -->
        {#if health.state === "running"}
          <div class="server-meta">
            {#if instSnippet(health.initialize?.instructions)}
              <div class="inst-snippet" title="Instructions snippet">
                {instSnippet(health.initialize?.instructions)}
              </div>
            {/if}
            <div class="caps">
              {#if !!health.initialize?.capabilities?.resources}
                <span class="badge cap" title="Server exposes resources">resources</span>
                {#if health.initialize?.capabilities?.resources?.subscribe === true}
                  <span class="badge cap" title="Supports resources.subscribe">subscribe</span>
                {/if}
                {#if health.initialize?.capabilities?.resources?.listChanged === true}
                  <span class="badge cap" title="Supports resources.listChanged">listChanged</span>
                {/if}
              {:else}
                <span class="badge cap" title="No resources">no resources</span>
              {/if}
            </div>
          </div>
        {:else if health.state === "failed" && health.error}
          <div class="server-meta"><div class="err">Error: {health.error}</div></div>
        {/if}
      </div>
    {/each}
  {:else}
    <div class="empty">None</div>
  {/if}
</div>

{#if showInfoModal && infoServer}
  <ModalBackdrop label="Close MCP server info" onClose={() => (showInfoModal = false)}>
    <div class="modal" role="dialog" aria-modal="true" aria-label="MCP server info">
      <header>
        <div style="display:flex; align-items:center; gap:0.5rem;">
          <span class="dot {infoServer?.state === 'running' ? 'on' : 'off'}"></span>
          <strong style="font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, 'Liberation Mono', monospace;"
            >{infoServer?.name}</strong
          >
          <span class="muted">{infoServer?.state}</span>
        </div>
      </header>
      <div class="body">
        {#if infoServer?.error}
          <div class="err">Error: {infoServer.error}</div>
        {/if}
        <div class="row">
          <div style="margin-left:auto;">
            <strong>Tools:</strong>
            {Array.isArray(infoServer?.tools) ? infoServer.tools.length : 0}
          </div>
        </div>

        {#if infoServer?.initialize}
          {@const init = infoServer.initialize}
          <!-- Protocol details (version) not provided by InitializeResult; omit for now. -->

          {#if init?.instructions}
            <section>
              <h5>Instructions</h5>
              <pre class="pre">{init.instructions}</pre>
            </section>
          {/if}

          {#if init?.serverInfo}
            <section>
              <h5>Server Info</h5>
              <div use:jsonView={init.serverInfo}></div>
            </section>
          {/if}

          {@const capsView = init?.capabilities}
          {#if capsView}
            <section>
              <h5>Capabilities</h5>
              <div use:jsonView={capsView}></div>
            </section>
          {/if}
        {/if}

        <section>
          <h5>Available Tools</h5>
          {#if Array.isArray(infoServer?.tools) && infoServer.tools.length}
            <div class="tools-list modal-tools">
              {#each infoServer.tools as tool (tool.name)}
                {@const tkey = `${infoServer.name}:${tool?.name ?? ""}`}
                <div class="tool-item">
                  <button type="button" class="tool-header" on:click={() => toggleModalTool(tkey)}>
                    <span class="disclosure">{modalToolExpanded.get(tkey) ? "▼" : "▶"}</span>
                    <span class="tool-name">{tool?.name || "(unnamed tool)"}</span>
                  </button>
                  {#if modalToolExpanded.get(tkey)}
                    <div class="tool-details">
                      {#if tool?.description}
                        <pre class="tool-description">{tool.description}</pre>
                      {/if}
                      <div class="tool-schema">
                        <div class="schema-label">Parameters:</div>
                        <div use:jsonView={tool?.inputSchema}></div>
                      </div>
                    </div>
                  {/if}
                </div>
              {/each}
            </div>
          {:else}
            <div class="empty">None</div>
          {/if}
        </section>
      </div>
      <footer>
        <button class="secondary" on:click={() => (showInfoModal = false)}>Close</button>
      </footer>
    </div>
  </ModalBackdrop>
{/if}

<style>
  .manage {
    margin-bottom: 0.5rem;
  }
  .manage-body {
    padding: 0.5rem;
    border: 1px solid var(--border);
    background: var(--surface-2);
  }
  .field .inline {
    white-space: nowrap;
    color: var(--muted);
    font-size: 0.85rem;
  }
  /* Override full-width controls inside inline fields */
  .field input[type="text"],
  .field select {
    width: auto;
  }
  .field input[type="text"] {
    flex: 1;
    min-width: 0;
  }
  .grow {
    display: flex;
    flex-direction: column;
    width: 100%;
  }
  textarea {
    width: 100%;
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
    font-size: 0.85rem;
  }
  input[type="text"],
  select {
    width: 100%;
  }
  .preview {
    margin-top: 0.5rem;
  }
  .servers h4 {
    margin: 0.25rem 0;
  }
  .server-item {
    margin: 0.25rem 0;
  }
  .server-header-row {
    display: flex;
    align-items: center;
    gap: 0.25rem;
  }
  .server-header,
  .tool-header {
    padding: 0.25rem;
    display: flex;
    align-items: center;
    gap: 0.5rem;
    user-select: none;
  }
  .server-header {
    flex: 1;
    text-align: left;
    cursor: pointer;
  }
  .server-header:hover,
  .tool-header:hover {
    background: var(--surface-2);
  }
  .disclosure {
    display: none;
  }
  .server-name {
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
    font-size: 0.85rem;
    font-weight: 500;
  }
  .tool-count {
    color: var(--muted);
    font-size: 0.75rem;
    margin-left: auto;
  }
  .small {
    font-size: 0.75rem;
    padding: 0.15rem 0.4rem;
  }
  .detach-btn {
    align-self: center;
    white-space: nowrap;
    display: inline-flex;
    align-items: center;
    gap: 0.25rem;
    padding: 0.15rem 0.4rem;
    background: transparent;
    border: 1px solid var(--border);
    border-radius: 6px;
    color: var(--text);
  }
  .detach-btn:hover {
    background: var(--surface-2);
  }
  .detach-btn svg {
    width: 14px;
    height: 14px;
  }
  .tools-list {
    margin-left: 1rem;
    border-left: 1px solid var(--border);
    padding-left: 0.5rem;
  }
  .tool-item {
    margin: 0.25rem 0;
  }
  .tool-name {
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
    font-size: 0.8rem;
  }
  .tool-details {
    margin-left: 1.5rem;
    margin-top: 0.25rem;
    padding: 0.5rem;
    background: var(--surface-2);
    border-radius: 0.25rem;
  }
  .tool-description {
    color: #555;
    font-size: 0.8rem;
    margin: 0 0 0.5rem 0;
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
    white-space: pre-wrap;
    word-break: break-word;
    overflow-wrap: anywhere;
  }
  .schema-label {
    font-weight: 500;
    font-size: 0.75rem;
    margin-bottom: 0.25rem;
    color: var(--muted);
  }
  .tool-schema {
    font-size: 0.75rem;
  }
  /* server-actions removed; detach now lives in header row */

  .server-meta {
    margin: 0.2rem 0 0.35rem 1.5rem;
    display: flex;
    flex-wrap: wrap;
    gap: 0.25rem 0.75rem;
    align-items: center;
  }
  .server-meta .inst-snippet {
    font-size: 0.8rem;
    color: var(--muted);
    max-width: 100%;
  }

  .badge {
    display: inline-flex;
    align-items: center;
    border: 1px solid var(--border);
    border-radius: 999px;
    padding: 0.05rem 0.4rem;
    font-size: 0.7rem;
    margin-left: 0.25rem;
    color: var(--muted);
  }

  /* Modal styles */
  /* Backdrop styling moved to ModalBackdrop component */
  .modal {
    background: var(--surface);
    color: var(--text);
    width: min(1000px, 92vw);
    max-height: 90vh;
    border: 1px solid var(--border);
    border-radius: 6px;
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.25);
    display: flex;
    flex-direction: column;
  }
  .modal .body {
    padding: 0.75rem;
    display: grid;
    grid-template-columns: 1fr;
    gap: 0.75rem;
    overflow: auto;
  }
  .modal .row {
    display: flex;
    gap: 1rem;
    align-items: center;
  }
  .modal h5 {
    margin: 0.25rem 0;
  }
  /* Removed unused .kv styles */
  .modal-tools {
    margin-left: 0;
    border-left: none;
    padding-left: 0;
  }
  .modal .disclosure {
    display: inline;
    font-size: 0.75rem;
    width: 1rem;
    flex-shrink: 0;
  }
  /* info button removed; row click opens modal */
</style>

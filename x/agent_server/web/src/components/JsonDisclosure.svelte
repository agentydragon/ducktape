<script lang="ts">
  // @ts-expect-error - json-formatter-js ships no type declarations
  import JSONFormatter from "json-formatter-js";
  import { onMount } from "svelte";

  // Minimal structural type for the untyped json-formatter-js default export.
  type JsonFormatterCtor = new (
    json: unknown,
    open?: number,
    config?: { theme?: string; hoverPreviewEnabled?: boolean }
  ) => { render(): HTMLElement };

  export let label: string;
  export let value: unknown = null;
  export let open: boolean = false;
  export let persistKey: string | null = null;

  let openState: boolean = open;

  onMount(() => {
    if (persistKey) {
      try {
        const raw = localStorage.getItem(`jsonDisclosure:${persistKey}`);
        if (raw === "true" || raw === "false") openState = raw === "true";
      } catch {
        // Ignore localStorage errors - disclosure state is not critical
      }
    }
  });
  function onToggle(e: Event) {
    const el = e.currentTarget as HTMLDetailsElement;
    openState = !!el.open;
    if (persistKey) {
      try {
        localStorage.setItem(`jsonDisclosure:${persistKey}`, String(openState));
      } catch {
        // Ignore localStorage errors - disclosure state is not critical
      }
    }
  }

  function jsonView(node: HTMLElement, val: unknown) {
    const prefersDark =
      typeof window !== "undefined" && window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
    const render = (v: unknown) => {
      node.innerHTML = "";
      let parsed: unknown = null;
      if (v && typeof v === "object") parsed = v;
      else if (typeof v === "string") {
        try {
          parsed = JSON.parse(v);
        } catch {
          parsed = null;
        }
      }
      if (parsed && typeof parsed === "object") {
        const Formatter = JSONFormatter as JsonFormatterCtor;
        const fmt = new Formatter(parsed, 1, {
          theme: prefersDark ? "dark" : undefined,
          hoverPreviewEnabled: true,
        });
        node.appendChild(fmt.render());
      } else {
        const pre = document.createElement("pre");
        pre.className = "pre";
        pre.textContent = typeof v === "string" ? v : String(v);
        node.appendChild(pre);
      }
    };
    render(val);
    return { update: (nv: unknown) => render(nv) };
  }
</script>

<details bind:open={openState} class="json-disclosure" on:toggle={onToggle}>
  <summary>{label}</summary>
  <div class="json-body" use:jsonView={value}></div>
</details>

<style>
  .json-disclosure {
    margin: 0.25rem 0;
  }
  .json-body :global(pre) {
    margin: 0;
  }
</style>

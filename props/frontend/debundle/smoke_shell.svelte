<script lang="ts">
  // Smoke-bundle UI shell. Mirrors the production `App.svelte` chrome
  // (header + nav + routed main area) closely enough for the load
  // test to assert against the same selectors (`nav`, `<h1>Props</h1>`,
  // nav links), but without the production `App.svelte`'s dependency
  // on backend-fed pages — those would 404 against the static-file
  // server and pollute `console.error`.
  //
  // The shell exists in the debundle subpackage rather than reusing
  // the production `App.svelte` because the production library is
  // package-private; the smoke needs a self-contained entry under its
  // own package.
  import { pathname, resolve } from "$lib/router";
  import { highlightLines } from "$lib/highlighting";
  import { DataTable } from "@careswitch/svelte-data-table";

  const navItems = [
    { path: "/", label: "Overview" },
    { path: "/runs", label: "Runs" },
    { path: "/snapshots", label: "Ground Truth" },
  ];

  function isActive(path: string, currentPath: string): boolean {
    if (path === "/") return currentPath === "/";
    return currentPath.startsWith(path);
  }

  // Force the smoke to actually exercise the two vendor packages so
  // the chunk graph emits them as real chunks (not eliminated by
  // tree-shaking), and so a runtime regression (e.g., wrapper-shape
  // mismatch breaking `hljs.highlight(...)`) surfaces as a console
  // error.
  const sampleLines = ["function hello(name) {", "  return `Hello, ${name}!`;", "}"];
  const highlighted = highlightLines(sampleLines, "javascript");

  const rows = [
    { id: 1, name: "alpha" },
    { id: 2, name: "beta" },
  ];
  const columns = [
    { id: "id" as const, key: "id" as const, name: "ID", sortable: true },
    { id: "name" as const, key: "name" as const, name: "Name", sortable: true },
  ];
  const dataTable = new DataTable({ data: rows, columns });
</script>

<div class="min-h-screen bg-gray-50 dark:bg-gray-900">
  <header class="bg-white dark:bg-gray-800 border-b border-gray-200 dark:border-gray-700 px-6 py-3">
    <div class="flex items-center justify-between">
      <div class="flex items-center gap-3">
        <h1 class="text-xl font-bold">
          <a href={resolve("/")} class="hover:text-blue-600">Props</a>
        </h1>
      </div>
      <nav class="flex gap-1">
        {#each navItems as { path, label } (path)}
          <a
            href={resolve(path)}
            class="px-3 py-1.5 rounded text-sm font-medium transition-colors
              {isActive(path, $pathname)
              ? 'bg-blue-100 text-blue-700 dark:bg-blue-900 dark:text-blue-300'
              : 'text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 hover:text-gray-900 dark:hover:text-gray-100'}"
          >
            {label}
          </a>
        {/each}
      </nav>
    </div>
  </header>

  <main class="p-6">
    <p data-debundle-smoke="route">Route: <span data-debundle-smoke="pathname">{$pathname}</span></p>
    <pre data-debundle-smoke="highlighted">{#each highlighted as line}{@html line}<br />{/each}</pre>
    <p data-debundle-smoke="datatable-rows">{dataTable.rows.length} row(s)</p>
  </main>
</div>

<script lang="ts">
  import { ChevronRight, ChevronDown, Folder, FolderOpen } from "lucide-svelte";
  import { SvelteSet } from "svelte/reactivity";
  import type { FileTreeNode } from "../lib/api/client";
  import { getFileIcon } from "../lib/fileTypes";

  interface Props {
    nodes: FileTreeNode[];
    onFileClick: (_: string) => void;
    selectedPath?: string;
  }

  let { nodes, onFileClick, selectedPath }: Props = $props();

  let expanded = $state(new SvelteSet<string>());

  function toggleExpand(path: string) {
    if (expanded.has(path)) {
      expanded.delete(path);
    } else {
      expanded.add(path);
    }
  }
</script>

{#snippet treeNode(node: FileTreeNode, depth: number)}
  {@const isExpanded = expanded.has(node.path)}
  {@const isSelected = selectedPath === node.path}
  {@const indent = depth * 16}
  {@const FileIcon = getFileIcon(node.name)}

  <div
    class="flex items-center gap-1 px-2 py-1 hover:bg-gray-100 dark:hover:bg-gray-800 cursor-pointer text-sm {isSelected
      ? 'bg-blue-100 dark:bg-blue-900'
      : ''}"
    style="padding-left: {indent + 8}px"
    role="button"
    tabindex="0"
    onclick={() => {
      if (node.is_dir) {
        toggleExpand(node.path);
      } else {
        onFileClick(node.path);
      }
    }}
    onkeydown={(e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        if (node.is_dir) {
          toggleExpand(node.path);
        } else {
          onFileClick(node.path);
        }
      }
    }}
  >
    {#if node.is_dir}
      <span class="text-gray-400 dark:text-gray-500">
        {#if isExpanded}
          <ChevronDown size={16} />
        {:else}
          <ChevronRight size={16} />
        {/if}
      </span>
      <span class="text-blue-500 dark:text-blue-400">
        {#if isExpanded}
          <FolderOpen size={16} />
        {:else}
          <Folder size={16} />
        {/if}
      </span>
    {:else}
      <span class="text-gray-400 dark:text-gray-500">
        <FileIcon size={16} />
      </span>
    {/if}
    <span class="flex-1 font-mono">{node.name}</span>
    {#if node.tp_count > 0 || node.fp_count > 0}
      <div class="flex items-center gap-1 text-xs">
        {#if node.tp_count > 0}
          <span
            class="px-1.5 py-0.5 bg-green-100 dark:bg-green-900 text-green-700 dark:text-green-300 rounded font-medium"
          >
            {node.tp_count} TP
          </span>
        {/if}
        {#if node.fp_count > 0}
          <span class="px-1.5 py-0.5 bg-red-100 dark:bg-red-900 text-red-700 dark:text-red-300 rounded font-medium">
            {node.fp_count} FP
          </span>
        {/if}
      </div>
    {/if}
  </div>

  {#if node.is_dir && isExpanded && node.children}
    {#each node.children as child (child.path)}
      {@render treeNode(child, depth + 1)}
    {/each}
  {/if}
{/snippet}

<div class="border dark:border-gray-700 rounded bg-white dark:bg-gray-900">
  {#each nodes as node (node.path)}
    {@render treeNode(node, 0)}
  {/each}
</div>

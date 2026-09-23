import { useState } from "react";
import { UnstyledButton } from "@mantine/core";
import { ChevronDown, ChevronRight, Folder, FolderOpen } from "../lib/icons";
import type { FileTreeNode } from "../lib/api/client";
import { getFileIcon } from "../lib/fileTypes";

interface Props {
  nodes: FileTreeNode[];
  onFileClick: (path: string) => void;
  selectedPath?: string;
}

export default function FileTree({ nodes, onFileClick, selectedPath }: Props) {
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());

  function toggleExpand(path: string) {
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  }

  function renderNode(node: FileTreeNode, depth: number) {
    const isExpanded = expanded.has(node.path);
    const isSelected = selectedPath === node.path;
    const FileIcon = getFileIcon(node.name);

    return (
      <div key={node.path}>
        <UnstyledButton
          type="button"
          className={`flex items-center gap-1 px-2 py-1 hover:bg-gray-100 dark:hover:bg-gray-800 cursor-pointer text-sm ${
            isSelected ? "bg-blue-100 dark:bg-blue-900" : ""
          }`}
          style={{ paddingLeft: depth * 16 + 8 }}
          aria-expanded={node.is_dir ? isExpanded : undefined}
          aria-current={isSelected ? "true" : undefined}
          onClick={() => {
            if (node.is_dir) toggleExpand(node.path);
            else onFileClick(node.path);
          }}
        >
          {node.is_dir ? (
            <>
              <span className="text-gray-400 dark:text-gray-500">
                {isExpanded ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
              </span>
              <span className="text-blue-500 dark:text-blue-400">
                {isExpanded ? <FolderOpen size={16} /> : <Folder size={16} />}
              </span>
            </>
          ) : (
            <span className="text-gray-400 dark:text-gray-500">
              <FileIcon size={16} />
            </span>
          )}
          <span className="flex-1 font-mono">{node.name}</span>
          {node.tp_count > 0 || node.fp_count > 0 ? (
            <span className="flex items-center gap-1 text-xs">
              {node.tp_count > 0 ? (
                <span className="px-1.5 py-0.5 bg-green-100 dark:bg-green-900 text-green-700 dark:text-green-300 rounded font-medium">
                  {node.tp_count} TP
                </span>
              ) : null}
              {node.fp_count > 0 ? (
                <span className="px-1.5 py-0.5 bg-red-100 dark:bg-red-900 text-red-700 dark:text-red-300 rounded font-medium">
                  {node.fp_count} FP
                </span>
              ) : null}
            </span>
          ) : null}
        </UnstyledButton>
        {node.is_dir && isExpanded && node.children ? (
          <div>{node.children.map((child) => renderNode(child, depth + 1))}</div>
        ) : null}
      </div>
    );
  }

  return (
    <div className="border dark:border-gray-700 rounded bg-white dark:bg-gray-900">
      {nodes.map((node) => renderNode(node, 0))}
    </div>
  );
}

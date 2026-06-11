export const TOOL_RESOURCES_LIST = "resources_list";
export const TOOL_RESOURCES_READ = "resources_read";

export const TOOL_LEAN_BROWSER_SEARCH = "lean_browser_search";
export const TOOL_LEAN_BROWSER_FIND = "lean_browser_find_in_page";
export const TOOL_LEAN_BROWSER_OPEN = "lean_browser_open_page";
export const TOOL_LEAN_BROWSER_INFO = "lean_browser_get_session_info";
export const TOOL_LEAN_BROWSER_BACK = "lean_browser_navigate_back";

export const COLLAPSED_TOOL_KEYS = [
  TOOL_RESOURCES_LIST,
  TOOL_RESOURCES_READ,
  TOOL_LEAN_BROWSER_SEARCH,
  TOOL_LEAN_BROWSER_FIND,
  TOOL_LEAN_BROWSER_OPEN,
  TOOL_LEAN_BROWSER_INFO,
  TOOL_LEAN_BROWSER_BACK,
] as const;

export const COLLAPSED_TOOL_SET = new Set<string>(COLLAPSED_TOOL_KEYS);

export const LEAN_BROWSER_LABELS: Record<string, string> = {
  [TOOL_LEAN_BROWSER_SEARCH]: "Lean Browser Search",
  [TOOL_LEAN_BROWSER_FIND]: "Lean Browser Find in Page",
  [TOOL_LEAN_BROWSER_OPEN]: "Lean Browser Open Page",
  [TOOL_LEAN_BROWSER_INFO]: "Lean Browser Get Session Info",
  [TOOL_LEAN_BROWSER_BACK]: "Lean Browser Navigate Back",
};

export function isCollapsedToolKey(name: string): boolean {
  return COLLAPSED_TOOL_SET.has(name);
}

export function isLeanBrowserTool(name: string): boolean {
  return name in LEAN_BROWSER_LABELS;
}

# TODO / Future Enhancements

## Rendering Improvements

- [ ] Make progress bars auto-expand to fill available space
  - Progress bars should automatically fill all remaining space after minimal-width columns
  - All other columns (tree, counts, percentages) should be minimal width

- [ ] Format large numbers more compactly (e.g., "+123456" as "+123k")

- [ ] Adaptive tree indentation
  - Balance compact display with information density
  - Dynamically adjust indent size (1-4 spaces) based on terminal width
  - Maintain preferred minimum progress bar size
  - Ensure progress bars remain useful and visible

- [x] Minimum visible progress bar sliver

## Features

- [x] Integration as git pager (automatic detection) - reads from stdin
- [ ] Support for binary files visualization
- [ ] Color scheme customization
- [ ] Different tree styles (ascii, unicode, etc.)

### Interactive Mode
- [ ] Add interactive mode that lets you expand/collapse tree nodes interactively
  - Use rich's Live display for real-time updates
  - Keyboard navigation (arrow keys, enter to expand/collapse)
  - Vi-style keybindings (j/k for navigation, space/enter for toggle)
  - Search functionality (/ to search, n/N for next/prev)
  - Toggle between different column views on the fly

### Box-shaped Hierarchical View
- [ ] Add box-shaped directory hierarchy view (like ncdu, WinDirStat)
  - Boxes sized proportionally to diff size (additions + deletions)
  - Nested boxes respect directory hierarchy
  - Use Rich's Box drawing or custom rendering
  - Color-coded by change type (green for additions, red for deletions, mixed for both)
  - Mouse support for navigation
  - Optional: treemap-style layout

### Filtering and Cutoff Options
- [ ] Add cutoff by top N items
  - `--top N` flag to show only top N files by change count
  - Show "... and N more files" summary at bottom
- [ ] Add percentage-based cutoff
  - `--min-percent PERCENT` to filter out files with <X% of total changes
  - Default could be 1% to hide noise
  - Show total changes hidden in summary
- [ ] Combine filters (e.g., top 10 OR >=1%)

### Other Enhancements
- [ ] Support for renamed files (currently shown as separate add/delete)
- [ ] Colored diff pass-through mode (like delta)
  - Show tree summary at top
  - Then pass through syntax-highlighted diff below
- [ ] Configuration file support (~/.config/git-diff-tree/config.toml)
- [ ] Git alias setup helper (`git-diff-tree --install-alias`)
- [ ] Performance optimization for large diffs (>1000 files)

## Code Quality

- [x] Use Rich's built-in Tree widget instead of custom tree rendering
- [ ] Consider using existing diff parsers (unidiff, whatthepatch) for more complex diff formats

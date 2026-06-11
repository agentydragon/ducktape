# TODO / Future Enhancements

## Rendering Improvements

- [ ] Implement columns as proper Rich renderables with measurement hints
  - **Current approach limitations**:
    - Using Table.grid() with implicit width constraints (`no_wrap`, `max_width`, `ratio`)
    - Working around Table's layout algorithm rather than with it
    - Hard to implement adaptive features (like flexible indent) without knowing available width
    - Width calculations are implicit, not explicit
  - **Custom Renderable benefits**:
    - Implement `__rich_measure__()` to report precise min/max width requirements
    - Implement `__rich_console__()` for adaptive rendering based on actual available width
    - Better guarantees: "tree needs ≥X chars, bars need ≥Y chars"
    - Enable adaptive features: measure available width → decide indent level → render
    - More predictable layout behavior across different terminal widths
  - **Implementation approach**:
    - Create custom Renderable classes: TreeColumn, CountsColumn, BarsColumn, PercentColumn
    - Each reports its measurement requirements via `__rich_measure__()`
    - DiffTree composes them with explicit width distribution logic
    - Pass available width down to components for adaptive rendering
  - **Tradeoffs**:
    - More complex code (need to understand Rich's rendering protocol)
    - More testing required (custom renderables harder to test)
    - More maintenance burden
    - But: enables robust adaptive features and better layout guarantees
  - **Enables future features**:
    - Flexible tree indent based on measured available width
    - Better handling of very narrow terminals
    - Smooth degradation of display quality as width decreases

- [ ] Format large numbers more compactly (e.g., "+123456" as "+123k")

- [ ] Adaptive tree indentation
  - Balance compact display with information density
  - Dynamically adjust indent size (1-4 spaces) based on terminal width
  - Maintain preferred minimum progress bar size
  - Ensure progress bars remain useful and visible

- [ ] Add flex in tree indent based on available width
  - Dynamically adjust tree indent from +1 to +3 spaces (current: fixed at +3)
  - Also vary number of "──" horizontal connector chars (1-2 currently, could be 1-3)
  - Reduce indent at narrow widths to save horizontal space
  - Use full +3 indent when width allows for better readability
  - Example:

    ```
    # Wide terminal (indent=3, connectors=2):
    root
    ├── src/module
    │   ├── file1.py
    │   └── file2.py
    └── test.py

    # Narrow terminal (indent=1, connectors=1):
    root
    ├─ src/module
    │ ├─ file1.py
    │ └─ file2.py
    └─ test.py
    ```

  - Challenge: must coordinate with multiple flexible elements:
    - Path collapsing (single-child directory merging creates variable path lengths)
    - Bar width constraints (bars already have max_width flexibility)
    - Tree column ellipsis behavior
    - Tree decoration characters (both vertical spacing and horizontal connectors)
  - Need to balance all flexibilities to avoid conflicts
  - Note: Rich's Tree class controls tree decoration - may need custom rendering

## Features

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
  - Git provides rename detection via `git diff --find-renames` (or `-M`)
  - With `-M`, `git diff --numstat` shows renames as: `old_path => new_path` or `{old => new}_common_path`
  - Implementation approaches:
    1. **Parse rename syntax from git** (recommended)
       - Use `git diff -M --numstat` instead of plain `--numstat`
       - Parse `=>` syntax: `file.py => renamed.py` or `src/{old => new}/file.py`
       - Show as single entry with special indicator (e.g., `old → new`)
       - Display combined stats (since rename often includes modifications)
    2. **Post-process add/delete pairs**
       - Detect matching content hashes between deleted and added files
       - More complex, requires additional git queries
       - Less reliable than git's built-in detection
  - **Tree placement**: Place renamed files under their **destination path** in tree
    - File appears at its new location, not old location
    - Show rename with arrow in filename
    - **Path display for source**: Use path relative to common ancestor or destination parent
      - Avoid redundant common prefixes
      - Keep it concise while being unambiguous
  - Example displays:

    ```
    # Simple case: different directories, different filenames
    lib/
    └── new.py ← src/old.py     +5  -2  ████ ██

    # Complex case: file moved within same parent (with path collapsing)
    lib/
    ├── bar/foo.py ← foo/foo.py     +3  -1  ██ █
    └── baz.py                       +2  -0  █
    # Note: bar/foo.py is collapsed since bar/ has only one child
    # Source path "foo/foo.py" is relative to lib/ (common parent)

    # Without collapsing: bar/ has multiple children
    lib/
    ├── bar/
    │   ├── foo.py ← foo/foo.py     +3  -1  ██ █
    │   └── new.py                   +2  -0  █
    └── baz.py                        +5  -2  ███ ██
    # Note: bar/ shown as directory since it has 2 children (foo.py + new.py)
    # Source path still relative to lib/

    # Mixed: renamed file + new file in same directory
    lib/
    ├── bar/
    │   ├── foo.py ← ../src/foo.py     +3  -1  ██ █
    │   └── new.py                      +2  -0  █
    └── baz.py                           +5  -2  ███ ██
    # Note: Source "../src/foo.py" goes up from lib/ to reach src/
    ```

  - Edge cases to handle:
    - Renamed + modified (most common)
    - Renamed with directory change
    - Partial renames (similarity threshold)
    - **Renames crossing diff scope boundary** (important):
      - When running `difftree` on a subdirectory (e.g., `git diff -- src/`)
      - File moved FROM current scope to outside: `src/old.py => lib/new.py`
        - Destination is outside tree scope, so **don't show it at all**
        - Or optionally show under a special "moved out" section at bottom
      - File moved TO current scope from outside: `lib/old.py => src/new.py`
        - Show at destination path (src/new.py) with indicator showing external source
        - Example: `src/new.py ← ../lib/old.py`
      - May need to track which paths are in scope and handle cross-boundary renames specially

- [ ] Colored diff pass-through mode (like delta)
  - Show tree summary at top
  - Then pass through syntax-highlighted diff below
- [ ] Configuration file support (~/.config/difftree/config.toml)
- [ ] Git alias setup helper (`difftree --install-alias`)
- [ ] Performance optimization for large diffs (>1000 files)

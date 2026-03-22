# difftree TODO

## Rendering

- [ ] Custom Rich renderables (replace `Table.grid()` workarounds with `__rich_measure__`/`__rich_console__` for proper width distribution)
- [ ] Compact large numbers (e.g., `+123k`)
- [ ] Adaptive tree indent (1-3 spaces based on terminal width; coordinate with path collapsing and bar width)

## Features

- [ ] Color scheme customization
- [ ] Tree style variants (ascii, unicode)
- [ ] Interactive mode (expand/collapse nodes, vi-style keys, search)
- [ ] Treemap/box view (like ncdu/WinDirStat, proportional to diff size)
- [ ] Filtering: `--top N`, `--min-percent PERCENT`

## Rename Support

Currently renames show as separate add/delete. Use `git diff -M --numstat` to detect renames and display as `new.py <- old.py` at destination path. Edge case: renames crossing diff scope boundary.

## Other

- [ ] Colored diff pass-through mode (tree summary + syntax-highlighted diff)
- [ ] Config file (`~/.config/difftree/config.toml`)
- [ ] `difftree --install-alias` helper
- [ ] Performance optimization for >1000 files

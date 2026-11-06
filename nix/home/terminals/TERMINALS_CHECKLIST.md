# Terminal Evaluation Checklist

Recorded results: ✓ = checked, ? = pending, — = not listed for that terminal.
Overall terminal, shortcut/UI, and theme evaluations remain pending for all three.
Add notes or rows as features and regressions are tested; these are not fresh checks.

| Check                                         | Kitty                                    | WezTerm                        | Ghostty                         |
| --------------------------------------------- | ---------------------------------------- | ------------------------------ | ------------------------------- |
| Tabs                                          | ? creation, moving, detaching            | ? native tab bar, stacking     | ? if available in current build |
| Open tab (`Ctrl+Shift+T`)                     | ✓                                        | ✓                              | ✓                               |
| Open window (`Ctrl+Shift+N`)                  | ?                                        | ✓                              | ?                               |
| Close tab (`Ctrl+Shift+W`)                    | ?                                        | ?                              | ?                               |
| Switch tab (`Ctrl+PageUp/PageDown`, no Shift) | ? match GNOME Terminal                   | —                              | —                               |
| Move tab (`Ctrl+Shift+PageUp/PageDown`)       | ? move left/right without changing focus | ?                              | ?                               |
| Scrollback (`Shift+PageUp/PageDown`)          | ? plus scrollbar hover; see note         | ? mouse wheel or scrollbar too | ? mouse wheel or scrollbar too  |
| Mouse scroll                                  | ✓ works without native scrollbar         | ? verify smooth scrolling      | ? verify smooth scrolling       |
| Selection and copy/paste (keyboard and mouse) | ?                                        | ?                              | ?                               |
| Search scrollback (`Ctrl+Shift+F`)            | ?                                        | ?                              | ?                               |
| Disable scroll-to-bottom on output            | ? see note                               | ?                              | ?                               |
| Light theme                                   | ? renders correctly                      | ? nix-colors palette           | ? preset loads                  |
| Dark theme                                    | ? renders correctly                      | ? nix-colors palette           | ? preset loads                  |
| Automatic theme switch                        | ? follows GNOME preference               | ? appearance hook              | ? config mapping                |
| OSC color preference signaling                | ? OSC 10/11/12 + 133/302                 | ?                              | ?                               |
| Scrollback/history and scrollbar              | ? history length and behavior            | ? rendering                    | ? buffer and style              |
| Splits/layouts                                | ? if using Kitty tiling                  | ? horizontal/vertical panes    | ? experimental support          |

Terminal-specific checks and notes:

- **Kitty:** no native scrollbar; rely on mouse/keyboard scrollback. It already stays
  in scrollback on output, with no explicit flag to disable scroll-to-bottom.
  Remote control/`kitty @` commands and theme watcher service status remain pending.
- **WezTerm:** Lua config hot-reload/events and multiplexing/SSH agent shim behavior
  remain pending.
- **Ghostty:** GPU/OpenGL requirements under nixGL, Wayland, server mode and other
  extra features remain pending.

# TODO / Future Enhancements

## Rendering Improvements

- [ ] Make progress bars auto-expand to fill available space
  - Progress bars should automatically fill all remaining space after minimal-width columns
  - All other columns (tree, counts, percentages) should be minimal width

- [ ] Format large numbers more compactly (e.g., "+123456" as "+123k")

## Features

- [ ] Support for binary files visualization
- [ ] Color scheme customization
- [ ] Different tree styles (ascii, unicode, etc.)
- [ ] Export to HTML/SVG
- [ ] Integration as git pager (automatic detection)

## Code Quality

- [ ] Use Rich's built-in Tree widget instead of custom tree rendering
- [ ] Consider using existing diff parsers (unidiff, whatthepatch) for more complex diff formats

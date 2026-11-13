# Excalidraw residual probes after core recovery

The November 13 extraction round retained the passing element constructors,
scene comparison/normalization/scroll helpers, and device resize helpers. Broader
source-validated candidates hit the existing 410-unit editor atomic group:

| Original home           | Selected units still in the residual |
| ----------------------- | ------------------------------------ |
| `element/mutateElement` | `1696` (`mutateElement`)             |
| `scene/selection`       | `1685` (`getSelectedElements`)       |
| `scene/comparisons`     | `1222` (`canChangeRoundness`)        |
| `element/heading`       | `1225` through `1236`                |
| `element/distance`      | `1274`                               |
| `element/sizeHelpers`   | `1699`, `1704`                       |
| `binaryheap`            | `1551` (`BinaryHeap`)                |

Unit IDs are scoped to the pinned editor chunk. The heap probe's two-edge
witness runs from `1551` to `1192` via sequencing and back via eager use. The
combined geometry probe crossed the residual, heading, distance and size-helper
destinations. These gate constraints do not prove an actual upstream
initialization cycle. The round retained no new assumptions and did not reopen
the deferred bounds reduction.

Initial collision and crop templates failed source matching; those probes do
not establish an ownership limitation. To continue them, repair the templates
against native AST output before running the normal gate.

The two extracted `transformHandles` device helpers import the browser entry.
Their runtime probe belongs in the existing browser smoke target, not in an
otherwise import-free Node geometry probe.

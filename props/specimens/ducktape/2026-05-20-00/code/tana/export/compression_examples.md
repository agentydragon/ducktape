# Tana Export Compression Strategies

## 1. **Remove Redundant Node IDs** (30-40% reduction)

- Keep only essential IDs for cross-references
- Remove IDs from leaf nodes that aren't referenced elsewhere
- Example: `[[buy a vise^2KoNzewiaEZz]]` → `buy a vise` (if not referenced)

## 2. **Consolidate Similar Items** (20-30% reduction)

- Group related tasks by category
- Merge duplicate/similar entries
- Example: Multiple "buy X" items → Single "Shopping: X, Y, Z"

## 3. **Remove Implementation Details** (15-20% reduction)

- Keep high-level goals, remove technical notes
- Example: Remove all the Google Assistant troubleshooting details

## 4. **Flatten Deep Hierarchies** (10-15% reduction)

- Reduce indentation levels where possible
- Combine parent-child relationships when semantically equivalent

## 5. **Use Shorthand Notation** (5-10% reduction)

- Replace verbose status markers
- Use symbols instead of text: ✓ instead of "completed"
- Abbreviate common terms: HA → Home Assistant

## Example Compressed Format

```
%%tana%%
- Root: uA_iLd0SUk
  - Glidepath #initiative
    • NFC: 2 stickers, QR fallback
    • Next: Threshold Tap, Calendar reminders, Charger-hook
    • Work phone apps: Waymo, Lyft
    • Rewards: 200pts→desktop, 400pts→trip
    • Track: DailyReps (2025-05-29 PM✓)

  - TODO Open (26 items)
    • Housing: PG&E rate, utilities rebalance, rent increase
    • Health: tinnitus, sleep, psychiatrist, stimulants, nightguard
    • Tech: pomodoro timer, work phone DND, flex alarm
    • Finance: Q1 federal tax, CA 2024 tax, US 2025 tax
    • Misc: backpack zipper, towel hooks, EU adapter

  - TODO Waiting (9 items)
    • Immigration: H1B extension, green card
    • Health: neuropsych testing, transfer providers
    • Tech: ThinkPad black-screen, toothbrush stalls

  - Shopping Categories
    • Electronics: oscilloscope, logic analyzer, USB chargers
    • Home: towel hooks, mail container, cleaning supplies
    • Personal: camping shoes, luggage, condoms
    • Tools: vise, PCB holder, calipers
```

## Compression Script Approach

```python
def compress_tana_export(content):
    # 1. Parse and build reference map
    # 2. Remove unreferenced IDs
    # 3. Consolidate similar items
    # 4. Apply shorthand replacements
    # 5. Flatten unnecessary hierarchy
    return compressed_content
```

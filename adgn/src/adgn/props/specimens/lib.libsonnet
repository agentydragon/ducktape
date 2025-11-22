// Jsonnet helpers for concise, DRY specimen issue definitions.
// Produces data compatible with adgn_llm.properties.specimen_issues.SpecimenIssues
// Usage (example):
//   local I = import 'specimens/lib.libsonnet';
//   I.root([
//     I.issueMultiFromLines(
//       id='iss-001',
//       rationale='Inline imports inside functions; move to module top.',
//       properties=['imports-top'],
//       linesByFile={
//         'wt/wt/cli.py': [101, 158, 193, 198, 206, 253],
//         'wt/wt/client/handlers.py': [10, 16, 50, 75, 86, 89, 94, 97, 104, 120, 127, 134, 136, 142, 152, [164,168], 194, 196, 201, 214, 220, 226, 238, 240, [242,243], 249, 254, 263, 277, 298, [301,302], 310, 342],
//       }
//     ),
//     I.issueSingle(id='iss-009', should_flag=false, rationale='shlex.quote requires str', files={ 'wt/wt/client/worktree_utils.py': [ 98 ] }),
//   ])


// Normalize a line spec into a LineRange object.
// Accepts either an int (single line) or a [start,end] array; also accepts objects that already have start_line/end_line.
local toRange(x) =
  if std.type(x) == 'number' then { start_line: x }
  else if std.type(x) == 'array' && std.length(x) == 2 then { start_line: x[0], end_line: x[1] }
  else if std.type(x) == 'object' && std.objectHas(x, 'start_line') then x
  else error 'Invalid line spec: ' + std.manifestJson(x);

// Normalize an array of mixed line specs to LineRange[]
local normRanges(arr) = [toRange(x) for x in arr];

// Build a files mapping entry: file -> [LineRange...]
local fileEntry(file, ranges) = { [file]: normRanges(ranges) };

// Normalize a {file: [rangeSpec]|null} mapping into canonical {file: LineRange[]|null}
local normFiles(files) = {
  [f]: if files[f] == null then null else normRanges(files[f])
  for f in std.objectFields(files)
};

// Expand shorthand mapping {file: [entry,...]|null} into a list of Occurrence objects
// Entry forms supported (per occurrence):
// - number            → single line
// - [start, end]      → span
// - {range: <spec>, note: "..."} → range + occurrence-level note
// - {note: "..."}    → unspecified range for that file with an occurrence-level note
// If value is null or []: one occurrence with unspecified range for that file (no note)

// Occurrence constructor helper: normalize per-entry forms to { files: {file: ranges|null}, note?: string }
local occFromEntry(file, ln) =
  if ln == null then { files: { [file]: null } }
  else if std.type(ln) == 'string' then { files: { [file]: null }, note: ln }
  else if std.type(ln) == 'number' then { files: fileEntry(file, [ln]) }
  else if std.type(ln) == 'array' && std.length(ln) == 2 && std.type(ln[0]) == 'number' && std.type(ln[1]) == 'string' then
    { files: fileEntry(file, [ln[0]]), note: ln[1] }
  else if std.type(ln) == 'array' && std.length(ln) == 2 && std.type(ln[0]) == 'number' && std.type(ln[1]) == 'number' then
    { files: fileEntry(file, [{ start_line: ln[0], end_line: ln[1] }]) }
  else if std.type(ln) == 'array' && std.length(ln) == 3 && std.type(ln[0]) == 'number' && std.type(ln[1]) == 'number' && std.type(ln[2]) == 'string' then
    { files: fileEntry(file, [{ start_line: ln[0], end_line: ln[1] }]), note: ln[2] }
  else error 'Invalid entry in linesByFile for ' + file + ': ' + std.manifestJson(ln);

local instancesFromLinesByFile(linesByFile) = std.flattenArrays([
  (
    local v = linesByFile[file];
    if v == null || (std.type(v) == 'array' && std.length(v) == 0)
    then [{ files: { [file]: null } }]
    else [occFromEntry(file, ln) for ln in v]
  )
  for file in std.objectFields(linesByFile)
]);

// Issue constructors

// One occurrence that can span multiple files/ranges
// Parameters:
//   rationale: Full explanation of what's wrong and recommended fix
//   filesToRanges: Dict of file paths → array of line ranges
//   properties: Array of property IDs from props/ that this issue violates
//   gap_note: Documents gaps in property taxonomy - when finding relates to existing properties
//             but represents a generalizable principle deserving its own property definition.
//             Describe what property SHOULD exist to capture this pattern more precisely.
//   should_flag: Whether this should be flagged (default: true)
local issueOneOccurrence(rationale, filesToRanges, properties=[], gap_note=null, should_flag=true) = {
  should_flag: should_flag,
  rationale: rationale,
  properties: properties,
  gap_note: gap_note,
  instances: [{ files: normFiles(filesToRanges) }],
};

// Many occurrences (explicit list)
local issueWithOccurrences(rationale, occurrences, properties=[], gap_note=null, should_flag=true) = {
  should_flag: should_flag,
  rationale: rationale,
  properties: properties,
  gap_note: gap_note,
  instances: [
    // Each instance.files may be a {file: [ranges]|null} map; normalize arrays to LineRange
    { files: normFiles(inst.files) }
    for inst in occurrences
  ],
};

// Many occurrences, each single-file/single-range (built from shorthand mapping)
local issueOccurrencesFromLines(rationale, linesByFile, properties=[], gap_note=null, should_flag=true) =
  issueWithOccurrences(rationale=rationale, occurrences=instancesFromLinesByFile(linesByFile), properties=properties, gap_note=gap_note, should_flag=should_flag);

// Multi-occurrence issue built from a simple list of files → each file as an instance with unspecified range
local instancesFromFiles(filesList) = [{ files: { [f]: null } } for f in filesList];
// Treat as a special case of linesByFile with empty arrays (unspecified ranges per file)
local issueOccurrencesFromFiles(rationale, filesList, properties=[], gap_note=null, should_flag=true) =
  issueOccurrencesFromLines(
    rationale=rationale,
    linesByFile={ [f]: [] for f in filesList },
    properties=properties,
    gap_note=gap_note,
    should_flag=should_flag,
  );

// Root wrapper for SpecimenIssues
local root(items) = { items: items };

// New v2 schema: embed source/scope (replaces YAML frontmatter)
local sourceGit(url, ref) = { vcs: 'git', url: url, ref: ref };
local sourceGitHub(org, repo, ref) = { vcs: 'github', org: org, repo: repo, ref: ref };
local sourceLocal(root='.') = { vcs: 'local', root: root };
local scope(include, exclude=null) = { include: include, exclude: exclude };

// Root wrapper v2 with manifest-style fields co-located with items (source/scope above items)
local rootV2(source, scope, items) = {
  source: source,
  scope: scope,
  items: items,
};

{
  // exported symbols
  issueOneOccurrence: issueOneOccurrence,
  issueWithOccurrences: issueWithOccurrences,
  issueOccurrencesFromLines: issueOccurrencesFromLines,
  issueOccurrencesFromFiles: issueOccurrencesFromFiles,
  // Legacy items-only root
  root: root,
  // V2 helpers
  sourceGit: sourceGit,
  sourceGitHub: sourceGitHub,
  sourceLocal: sourceLocal,
  scope: scope,
  rootV2: rootV2,
}

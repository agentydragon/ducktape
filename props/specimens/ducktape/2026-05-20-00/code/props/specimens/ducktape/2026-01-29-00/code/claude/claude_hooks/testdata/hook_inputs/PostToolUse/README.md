# Claude Tool Input Test Cases - PostToolUse Hook

Comprehensive list of all valid input combinations for Claude tools and their collection status.

**Claude Code Version:** 1.0.56

## Test Data Collection Methodology

- All test cases are empirically collected from actual Claude Code v1.0.56 hook invocations
- JSON structures represent real-world usage patterns and tool interactions
- Some values are paraphrased (e.g., shortened commands, anonymized paths) but structure and format remain equivalent to actual captured data
- Test data is organized in testdata/hook_inputs/PostToolUse/[ToolName]/[scenario].json
- Data may be anonymized for security but maintains functional equivalence

## Read Tool

- [x] file_path only ✅ (existing test)
- [x] file_path + limit ✅ (existing test)
- [x] file_path + offset ✅ (existing test)
- [x] file_path + offset + limit ✅ (existing test)

## Glob Tool

- [x] pattern only ✅ (testdata/Glob/pattern_only.json)
- [x] pattern + path ✅ (testdata/Glob/with_path.json)

## Grep Tool

- [x] pattern only ✅ (testdata/Grep/pattern_only.json)
- [ ] pattern + path
- [ ] pattern + glob
- [ ] pattern + type
- [ ] pattern + path + glob
- [ ] pattern + path + type
- [ ] pattern + glob + type
- [ ] pattern + path + glob + type
- [ ] ... + output_mode: "content"
- [ ] ... + output_mode: "files_with_matches"
- [ ] ... + output_mode: "count"
- [ ] ... + multiline: true
- [ ] ... + multiline: false
- [ ] ... + -i: true
- [ ] ... + -i: false
- [ ] ... + -n: true (content mode)
- [ ] ... + -n: false (content mode)
- [ ] ... + -A: number (content mode)
- [ ] ... + -B: number (content mode)
- [ ] ... + -C: number (content mode)
- [ ] ... + head_limit: number

## Edit Tool

- [x] file_path + old_string + new_string + replace_all: false ✅ (existing test)
- [ ] file_path + old_string + new_string + replace_all: true
- [ ] file_path + old_string + new_string (no replace_all)

## MultiEdit Tool

- [x] file_path + 1 edit {old_string, new_string, replace_all: false} ✅ (existing test)
- [ ] file_path + multiple edits
- [ ] edits with replace_all: true
- [ ] edits with no replace_all specified
- [ ] mixed replace_all values

## Write Tool

- [x] file_path + content ✅ (existing test)

## Bash Tool

- [x] command only ✅ (testdata/Bash/command_only.json)
- [x] command + description ✅ (testdata/Bash/with_timeout_and_description.json)
- [x] command + timeout ✅ (testdata/Bash/with_timeout_no_description.json)
- [x] command + description + timeout ✅ (testdata/Bash/with_timeout_and_description.json)

## LS Tool

- [x] path only ✅ (existing test)
- [x] path + ignore array ✅ (testdata/LS/with_ignore_array.json, testdata/LS/detailed_response.json)

## NotebookRead Tool

- [ ] notebook_path only
- [ ] notebook_path + cell_id

## NotebookEdit Tool

- [ ] notebook_path + new_source
- [ ] ... + cell_id
- [ ] ... + cell_type: "code"
- [ ] ... + cell_type: "markdown"
- [ ] ... + edit_mode: "replace"
- [ ] ... + edit_mode: "insert"
- [ ] ... + edit_mode: "delete"
- [ ] notebook_path + new_source + cell_id + cell_type
- [ ] notebook_path + new_source + cell_id + edit_mode
- [ ] notebook_path + new_source + cell_type + edit_mode
- [ ] notebook_path + new_source + cell_id + cell_type + edit_mode

## WebFetch Tool

- [x] url + prompt ✅ (testdata/WebFetch/basic.json)

## TodoWrite Tool

- [ ] empty todos array
- [ ] single complete todo object (all 4 fields)
- [x] multiple todos with mixed status/priority values ✅ (testdata/TodoWrite/basic.json)

## Task Tool

- [x] description + prompt ✅ (testdata/Task/basic.json)

## exit_plan_mode Tool

- [x] plan ✅ (testdata/exit_plan_mode/basic.json)

## MCP Tools (Unknown Tools - Fallback to dict)

- [x] Various MCP tools ✅ (existing test for mcp\_\_...)

## Notes

- Bash tool with timeout exceeded does not generate PostToolUse hook invocation
- Interrupted Bash tool calls do not generate PostToolUse hook invocation

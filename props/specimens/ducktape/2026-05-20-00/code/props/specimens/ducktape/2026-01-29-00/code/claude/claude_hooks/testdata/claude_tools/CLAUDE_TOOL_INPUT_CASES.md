# Claude Tool Input Test Cases

Comprehensive list of all valid input combinations for Claude tools.

**Claude Code Version:** 1.0.56

## Read Tool

- [ ] file_path only
- [ ] file_path + limit
- [ ] file_path + offset
- [ ] file_path + offset + limit

## Glob Tool

- [ ] pattern only
- [ ] pattern + path

## Grep Tool

- [ ] pattern only
- [ ] pattern + path
- [ ] pattern + glob
- [ ] pattern + type
- [ ] pattern + path + glob
- [ ] pattern + path + type
- [ ] pattern + glob + type
- [ ] pattern + path + glob + type
- [ ] + output_mode: "content"
- [ ] + output_mode: "files_with_matches"
- [ ] + output_mode: "count"
- [ ] + multiline: true
- [ ] + -i: true
- [ ] + -n: true (content mode)
- [ ] + -A: number (content mode)
- [ ] + -B: number (content mode)
- [ ] + -C: number (content mode)
- [ ] + head_limit: number

## Edit Tool

- [ ] file_path + old_string + new_string + replace_all: false
- [ ] file_path + old_string + new_string + replace_all: true
- [ ] file_path + old_string + new_string (no replace_all)

## MultiEdit Tool

- [ ] file_path + single edit {old_string, new_string}
- [ ] file_path + multiple edits
- [ ] mixed replace_all values (some true, some false, some omitted)

## Write Tool

- [ ] file_path + content

## Bash Tool

- [ ] command only
- [ ] command + description
- [ ] command + timeout
- [ ] command + description + timeout

## LS Tool

- [ ] path only
- [ ] path + ignore array

## NotebookRead Tool

- [ ] notebook_path only
- [ ] notebook_path + cell_id

## NotebookEdit Tool

- [ ] notebook_path + new_source
- [ ] + cell_id
- [ ] + cell_type: "code"
- [ ] + cell_type: "markdown"
- [ ] + edit_mode: "replace"
- [ ] + edit_mode: "insert"
- [ ] + edit_mode: "delete"
- [ ] notebook_path + new_source + cell_id + cell_type
- [ ] notebook_path + new_source + cell_id + edit_mode
- [ ] notebook_path + new_source + cell_type + edit_mode
- [ ] notebook_path + new_source + cell_id + cell_type + edit_mode

## WebFetch Tool

- [ ] url + prompt

## TodoWrite Tool

- [ ] empty todos array
- [ ] single complete todo object (all 4 fields)
- [ ] multiple todos with mixed status/priority values

## Task Tool

- [ ] description + prompt

## exit_plan_mode Tool

- [ ] plan

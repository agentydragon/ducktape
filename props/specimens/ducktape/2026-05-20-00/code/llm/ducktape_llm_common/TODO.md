# TODO

## Claude Linter Enhancements

### Add Pre-Hook Support for Edit/MultiEdit Tools

Currently, the pre-hook only checks Write operations. We need to extend it to also check Edit and MultiEdit operations for non-fixable violations in the edited sections.

**Tasks:**

- [ ] Update pre-hook to handle Edit tool
  - Extract the `new_string` content from Edit operations
  - Check only the new content for non-fixable violations
  - Report violations that would be introduced by the edit
- [ ] Update pre-hook to handle MultiEdit tool
  - For each edit in the `edits` array, extract the `new_string`
  - Check each new string for non-fixable violations
  - Report which specific edit would introduce violations
- [ ] Update hooks configuration in settings.json to add pre-hook matchers for Edit and MultiEdit
- [ ] Add tests for Edit/MultiEdit pre-hook functionality
  - Test blocking edits that would introduce S113 (missing timeout)
  - Test allowing edits with only fixable violations
  - Test multiple edits in MultiEdit with mixed violations

**Rationale:** This prevents Claude from introducing non-fixable violations when editing existing files, not just when creating new ones.

# Specimens TODO

### Cleanup

- [ ] Remove `lib.libsonnet` (no longer needed with YAML format)
- [ ] Remove legacy `.libsonnet` files in `ducktape/2025-11-20-00/todo/`

### Verification

- [ ] Run adgn tests to ensure specimens load correctly after migration
- [ ] Verify specimen hydration works: `adgn-properties snapshot exec ducktape/2025-11-26-00 -- ls -la`
- [ ] Check that critic runs still work with new YAML format

### Future Improvements

- [ ] Consider simplifying bundle mechanism (direct code storage vs git bundles)
- [ ] Add schema validation for YAML issue files
- [ ] Add automated pre-commit hooks for issue file validation

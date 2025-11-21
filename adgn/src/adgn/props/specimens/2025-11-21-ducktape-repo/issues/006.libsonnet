local I = import '../../specimens/lib.libsonnet';

// iss-006: Delete _setup_wrapper no-op method and its call site

I.issueOneOccurrence(
  rationale=|||
    The `_setup_wrapper` method (lines 578-585) is an explicit no-op kept "for future
    extensibility" but provides no current value and should be deleted.

    **Current code (lines 578-585):**
    ```python
    async def _setup_wrapper(self) -> None:
        """Set up or refresh any container-dependent wrapper state.

        Currently a no-op: we use a committed host-side wrapper script that only
        needs the container ID and docker binary provided via environment in
        receive_messages(). This hook is kept for future extensibility.
        """
        return
    ```

    **Call site (line 519):**
    ```python
    await self._setup_wrapper()
    ```

    **Why delete:**
    - **Explicit no-op**: Comment says "Currently a no-op"
    - **Speculative**: "kept for future extensibility" - YAGNI violation
    - **Single caller**: Only one await at line 519
    - **No value**: Does nothing currently, might never be needed
    - **Maintenance burden**: Keeping dead code requires mental overhead
    - **Misleading**: Reader might think it does something important

    **Comment analysis:**
    The comment explains: "we use a committed host-side wrapper script that only needs
    the container ID and docker binary provided via environment in receive_messages()."
    This means the functionality works WITHOUT this method - the method is truly unused.

    **"Future extensibility" anti-pattern:**
    - Don't keep empty methods "just in case" they might be needed later
    - If the need arises, add it then (git history preserves deleted code)
    - Empty hooks add complexity without benefit
    - Violates YAGNI (You Aren't Gonna Need It)

    **What to delete:**
    1. Method definition (lines 578-585)
    2. Call site (line 519): `await self._setup_wrapper()`

    **After deletion:**
    Line 519 context (lines 515-520):
    ```python
    if not self.use_git_volume:
        # Git volume disabled - no remounting needed
        self._logger.info("Git volume disabled - skipping remount")
        # Still need to setup the wrapper after container start
        await self._setup_wrapper()  # DELETE THIS LINE
        return
    ```

    Just delete line 519 and the comment on line 518 becomes incorrect. Update to:
    ```python
    if not self.use_git_volume:
        # Git volume disabled - no remounting needed
        self._logger.info("Git volume disabled - skipping remount")
        return
    ```

    **Benefits:**
    - Less code to maintain
    - No misleading no-ops
    - Clearer that nothing happens in this path
    - Can always restore from git if actually needed
  |||,
  properties=['dead-code', 'YAGNI', 'speculative-generality'],
  filesToRanges={
    'adgn/src/adgn/inop/runners/containerized_claude.py': [
      [578, 585],  // _setup_wrapper no-op method definition
      [519, 519],  // Call site to delete
      [518, 518],  // Comment that becomes incorrect after deletion
    ],
  },
)

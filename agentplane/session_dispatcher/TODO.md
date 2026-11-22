# Session dispatcher TODOs

- [ ] When central-sampler Sessions can exist without an Environment, revisit command and receipt retention,
      read authority and delivery fencing for those Sessions without relying on a Sandbox UID.
- [ ] Remove the redundant string representation of session-dispatcher state and operation. The
      dispatcher stores lowercase names (`submit_input`) and maps them by hand to protobuf enums in
      `agentplane/session_dispatcher/internal/store/snapshots.go`, which duplicates the enum. Store
      the proto enum value directly (e.g. an integer column) or another representation that needs no
      mapping table, with a migration rather than an edit to landed ones. Gate: the stored-name
      mapping can go once no row or writer uses the string form.

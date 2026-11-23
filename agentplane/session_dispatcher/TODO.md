# Session dispatcher TODOs

- [ ] When central-sampler Sessions can exist without an Environment, revisit command and receipt retention,
      read authority and delivery fencing for those Sessions without relying on a Sandbox UID.
- [ ] Finish the [ordered enum storage rollout](README.md#enum-storage-rollout).
      Complete and verify the numeric backfill before numeric readers or writers
      switch over. Retire the text columns as a separate deployment-gated change.

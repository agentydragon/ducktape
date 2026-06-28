# cluster/validation TODO

- **Detect SOPS secrets in a Flux Kustomization that lacks a `decryption` block.**
  A `Kustomization` whose `spec.path` contains a SOPS-encrypted file (a manifest
  with `ENC[AES256_GCM,...]` values / a `sops:` metadata block) but no
  `spec.decryption.provider: sops` makes Flux apply the **ciphertext literally** —
  the consumer gets `ENC[...]` instead of the secret, with no error. This bit the
  `vm-images-publisher` attic-reader netrc (silently applied as ciphertext until
  caught by hand). Consider a cluster-validation test that, for each
  `flux-kustomization.yaml`, scans the files under its `path` for SOPS markers and
  asserts the Kustomization declares `decryption.provider: sops` (+ the expected
  `secretRef`). Pairs well with the existing structural checks in
  `cluster/validation/`.

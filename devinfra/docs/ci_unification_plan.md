# Ansible-lint in Bazel

Full ansible-lint currently runs in its own GitHub Actions workflow because it
installs Ansible Galaxy roles and collections. The current CI split is documented
in [`linting.md`](linting.md).

## Open work

- [ ] Evaluate whether ansible-lint can and should run under Bazel/RBE. If the
      Galaxy dependency makes that a poor fit, record the independent workflow as
      the intended boundary in `linting.md` and remove this plan.

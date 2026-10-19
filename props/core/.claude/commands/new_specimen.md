# Create a new specimen

Scaffold a new specimen. Structure and file formats come from
`props/specimens/docs/format_spec.md` and `props/specimens/docs/authoring_guide.md`;
read them first and do not restate them here.

Ask for what the docs require and offer defaults: the source (in-repo commit,
or an external repo pinned with `http_archive` in `MODULE.bazel`), the
included and excluded paths, the project name, and the split.

Create `props/specimens/<project>/<YYYY-MM-DD-NN>/` with a `BUILD.bazel` calling
`specimen_targets()` (provenance as comments), the `code/` tree for an in-repo
source (rename nested `BUILD.bazel` files with the `.specimen` suffix), and an
empty `issues/` directory. Then run
`bbr test //props/specimens/<project>/<YYYY-MM-DD-NN>:test_specimen`.

# Can ESMeta check an emitted module graph?

[ESMeta 0.8.2](https://github.com/es-meta/esmeta/tree/b4a787f2716523bf76d4a897068bbd5b1fc8ef2d)
parsed the emitted entry from an early splitter example with
`-parse:goal=Module`, but `esmeta eval` treated it as a script and failed at
the import. A standalone script evaluated successfully. Node ran both the
original bundle and the four-module split with identical stdout.

Revisit ESMeta as a second runtime check when it can load, link and evaluate
a closed ES module graph with live imports. At the tested revision,
`HostLoadImportedModule` was a no-op; [upstream issue #372](https://github.com/es-meta/esmeta/issues/372)
tracks module support. Successful execution would provide evidence for that
example. Extending the [CoreJS semantics](../model/README.md) to ESMeta
would also require a checked correspondence between their semantics.

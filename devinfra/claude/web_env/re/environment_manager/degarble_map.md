# Garbled package map — environment-manager `0b86a2a0`

`environment-manager` is built with [garble](https://github.com/burrowers/garble),
which rewrites every non-stdlib package and identifier name to a random token.
The mapping below recovers what those tokens mean.

**The map is per-build.** Garble derives names from a build seed, so the tokens
here apply only to Build ID `0b86a2a0dbc9411eb18435e1c56822b0156f90fe`. A new
binary needs the map rebuilt — the method is stable, the names are not. (For
scale: the previous binary called the `cmd` package `FgSB6rLPg` and cobra
`QHh5pCW`; this one calls them `qqGXzsqMa` and `iY5hxRroU`.)

## How the map is derived

Garble cannot rename everything, and each surviving name is a fingerprint:

1. **Method names required for interface satisfaction survive.** A type that
   implements `io.Writer` must still have a method literally called `Write`, or
   the interface check fails at runtime. So `ServeHTTP`, `RoundTrip`,
   `MarshalJSON`, `ForceFlush`, `Collect` and friends all remain in the clear.
   This is the single highest-signal source: it identifies both third-party
   packages (by their well-known interfaces) and application packages (by their
   own internal interfaces).
2. **Stdlib package paths survive** — `internal/abi`, `runtime`, `sync`, `math`
   are untouched, so anything _not_ matching a stdlib path is app or vendor code.
3. **String literals cross-reference to functions.** See `xrefs` in the RE
   toolchain notes below.

Recovering the names at all requires rebuilding the symbol table first; see
`//skills/reverse_engineer/examples:gosymtab` and the "Defeating Obfuscation"
section of the `reverse_engineer` skill.

### What does not work: recovering file boundaries

`.gopclntab` carries a file table (95,432 entries here) and `PCToLine` maps
each function to its source file, which looks like a free recovery of the
package's file layout. It isn't. Garble randomizes the filename per function,
not per file, so obfuscated packages report roughly one "file" per two
functions — `TaVHwGAw` claims 358 source files for 827 functions. Stdlib
packages, which garble leaves alone, group correctly for comparison
(`runtime/proc.go` holds 201 functions). Treat any per-file grouping of app
code as an artifact; group by package and by receiver type instead.

## Application packages

These are the reverse-engineering targets — the Anthropic module
`github.com/anthropics/anthropic/api-go/environment-manager`.

| Garbled        | Funcs | Role                                  | Surviving methods (evidence)                                                                                                                                                                                                |
| -------------- | ----- | ------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `TaVHwGAw`     | 827   | Claude Code launcher + **warm spare** | `Claim`, `ClaimFailReason`, `SetSpare`, `SetSpareAdopted`, `SetWarmSpareClaimed`, `Adopted`, `SpawnedAt`, `ReadyObserved`, `Wait`, `WireOutput`, `SetClaudePath`, `SetStartTime`, `SetStartupEndTime`, `Destroy`, `Execute` |
| `JKzN_Mds`     | 904   | Git source processing                 | `ProcessSources`, `SetupGitProxyAfterSourcesProcessed`, `ValidateRepositoryAccess`, `UpdateRemoteURL(s)`, `SetGitMountBaseURL`, `RemoveStaleLoopbackGitConfig`, `CanHandle`, `Process`                                      |
| `qqGXzsqMa`    | 665   | `cmd` (Cobra subcommands)             | `GetEnvironmentForSession`, `GetAuthContext`, `GetOutcomes`; `main.main` calls 7 constructors here                                                                                                                          |
| `CWddODOS8sH`  | 423   | Session/environment init              | `SetStartupContext`, `SetAuthContext`, `SetSessionMode`, `Initialize`, `InitSteps`, `GetClaudeEnvironmentVariables`                                                                                                         |
| `oBeT9S`       | 319   | Git HTTP proxy                        | `ServeHTTP`, `GetProxyURL`, `BaseURL`, `IsRunning`                                                                                                                                                                          |
| `FKPKJ5B0zZ`   | 303   | Observability facade                  | `Shutdown`, `LogHandler`, `Tracer`, `Increment`, `RecordGauge`                                                                                                                                                              |
| `WOoacuN0`     | 248   | Setup / lease                         | Same init surface as `CWddODOS8sH` plus `CreateLeaseManager`, `RoundTrip`                                                                                                                                                   |
| `aiubXmT8l`    | 243   | Process exec helpers                  | `Execute`, `ExecuteWithStdin`, `SleepWithJitter`, `GetIdentity`                                                                                                                                                             |
| `WWD9Ee6Wrf4m` | 224   | Backend API client                    | `RegisterWorker`, `WorkerEpoch`, `Heartbeat`, `AcknowledgeWork`, `PostEvent`, `PostJSON`, `FlushLogs`, `OtlpEndpoints`, `StartHeartbeatBridge`, `RetryableHTTPDo`, `IsFatal`, `GetUserMessage`                              |
| `uBHoqupaaEIs` | ~100  | `internal/mcp` BaseServer (HTTP)      | `GetConfig`, `GetName`, `GetTools`, `ShouldRegisterWithClaude`, `Header`, `Write`, `WriteHeader`                                                                                                                            |
| `N4j_xy6`      | ~90   | codesign MCP server                   | `GetConfig`, `GetName`, `GetTools`, `ShouldRegisterWithClaude`                                                                                                                                                              |
| `kItfsbt_`     | ~60   | Source/repo descriptor                | `GetDirectory`, `GetType`, `IsRepository`, `IsHermeticMode`, `UsesWorkspaceRootCwd`, `Validate`                                                                                                                             |
| `JGHtMM`       | ~50   | Output tailer                         | `IsRunning`, `Lines`                                                                                                                                                                                                        |

`CWddODOS8sH` and `WOoacuN0` expose the same initialisation surface
(`SetStartupContext`, `SetSessionMode`, `InitSteps`, `Initialize`,
`GetClaudeEnvironmentVariables`) because they are the **two `envtype`
implementations**, not a package and a wrapper:

- `CWddODOS8sH` = `internal/envtype/anthropic` — its `Initialize` is 17,616
  disassembly lines and references `setup_script`, `clone`, and the language
  install targets `golang`/`node`/`nodejs`/`python`.
- `WOoacuN0` = `internal/envtype/byoc` — 4,589 lines, and `WOoacuN0.BJymDLy7`
  compares against the literal `byoc`.

The `byoc` literal is recovered from an inline comparison, not from `strings`:
it is an instruction operand, which `-literals` cannot encrypt. See
`//skills/reverse_engineer/examples:inline_strings`.

## Third-party and stdlib-adjacent packages

Identified from well-known interface methods and characteristic string literals.

| Garbled                                            | Package                                    |
| -------------------------------------------------- | ------------------------------------------ |
| `q9Y582tbRs`                                       | `net/http`                                 |
| `x3ZgH1`                                           | `net`                                      |
| `a7qm6M0`                                          | `crypto/tls`                               |
| `R5H0z_A`                                          | `crypto/x509`                              |
| `iY5hxRroU`                                        | `github.com/spf13/cobra`                   |
| `qrqXIVytjy4`                                      | `github.com/spf13/pflag`                   |
| `rRe0vDD8`                                         | `google.golang.org/grpc`                   |
| `Lb5FUD`, `wpHjalIkUe_d`, `ILlqzReWzJT`            | `google.golang.org/protobuf`               |
| `eO_kGefa`                                         | OTLP protobuf messages                     |
| `AP0zyxaeRB`, `JjkekE6pEa0`                        | `go.opentelemetry.io/otel/sdk/metric`      |
| `Vzx1w2MN`, `hXcfv273Gd`                           | `go.opentelemetry.io/otel/sdk/trace`       |
| `UhnIMikAa`                                        | OTel semconv / `otelconv`                  |
| `CQ2hyvF53X`, `AW508CpPB6tR`, `sjbEGw1`            | OTel semantic-convention attribute tables  |
| `rTTXfb`, `JHeDT05Xjn5`                            | `go.opentelemetry.io/otel/attribute`       |
| `nwTZT_5` (server), `Fx8FWIa6K` (mcp)              | `github.com/mark3labs/mcp-go` v0.54.1      |
| `qsgTku7Q`                                         | `github.com/santhosh-tekuri/jsonschema/v6` |
| `Bo9xYr`                                           | `github.com/google/jsonschema-go`          |
| `gFaqQE30LW8F`                                     | `gopkg.in/yaml.v3`                         |
| `MpjIWFtBo`, `MNg1oPg`, `ClhtFvLRCFXF`, `Eh4KwZna` | HTTP/2 + HPACK                             |
| `dZ8mSGBI`, `ByuAgsG7aP`, `_zpBFlgnXXt`            | `text/template`, `html/template`           |
| `br8Q6z0Nz7`                                       | `unicode` script tables                    |
| `_mIPuq8cdJd`                                      | DNS message parsing                        |
| `daI_d2D7`                                         | `os` file layer                            |

Both JSON Schema entries are genuinely separate libraries, and both arrive
transitively through mcp-go. `qsgTku7Q` is pinned to santhosh-tekuri/jsonschema
v6 by the struct tag `json:"AbsoluteKeywordLocation,omitempty"` — the capital
`A` is a quirk of v6's `output.go` that no other Go library shares — plus its
embedded `metaschemas/` tree and the `urn:mem:metaschema` URN. `Bo9xYr` is
google/jsonschema-go, identified by `ApplyDefaults`/`CloneSchemas`/`Resolve`
and the unexported field `resolvedRef`.

`invopop/jsonschema`, present in the previous binary, is **gone**: its only
consumer of `wk8/go-ordered-map/v2` (`Oldest`/`Newest`/`GetPair`/`AddPairs`)
appears in the old binary and in neither package of the new one.

## Reproducing the map

```bash
W=/path/to/workdir
gosymtab /opt/env-runner/environment-manager $W/em-new.sym
go tool nm $W/em-new.sym | awk '$2=="T"||$2=="t"{print "0x"$1, $3}' | sort > $W/funcs.txt

# Group by package, keep only identifiers garble left readable.
python3 - "$W/funcs.txt" <<'PY'
import re, sys, collections
def pkg(n):
    depth, out = 0, []
    for c in n:                      # strip generic instantiations first
        if c == '[': depth += 1
        elif c == ']': depth -= 1
        elif depth == 0: out.append(c)
    s = ''.join(out); sl = s.rfind('/'); tail = s[sl+1:]; dot = tail.find('.')
    return s if dot < 0 else s[:sl+1] + tail[:dot]

def readable(t):                     # a real word, not a garble token
    return (re.fullmatch(r'[A-Za-z][A-Za-z]{2,}', t)
            and len(re.findall(r'[aeiouAEIOU]', t)) >= 2)

methods = collections.defaultdict(set)
for line in open(sys.argv[1]):
    _, name = line.split(None, 1)
    tail = re.sub(r'\[.*?\]', '', name.strip()).split('.')[-1]
    if readable(tail):
        methods[pkg(name.strip())].add(tail)
for p, m in sorted(methods.items(), key=lambda kv: -len(kv[1])):
    print(f"{len(m):4d}  {p:18s} {' '.join(sorted(m)[:14])}")
PY
```

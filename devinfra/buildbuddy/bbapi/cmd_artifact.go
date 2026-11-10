package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"

	bbapipb "github.com/agentydragon/ducktape/devinfra/buildbuddy/bbapi/proto"
	bespb "github.com/buildbuddy-io/buildbuddy/proto/build_event_stream"
	"github.com/spf13/cobra"
	"google.golang.org/protobuf/proto"
)

// matchGlob reports whether s matches a simple glob pattern supporting '*' only.
func matchGlob(pattern, s string) bool {
	for {
		star := strings.IndexByte(pattern, '*')
		if star < 0 {
			return pattern == s
		}
		prefix := pattern[:star]
		if !strings.HasPrefix(s, prefix) {
			return false
		}
		s = s[len(prefix):]
		pattern = pattern[star+1:]
		if pattern == "" {
			// Trailing '*' matches the rest.
			return true
		}
		// Find the earliest occurrence of the literal text before the next '*'.
		nextStar := strings.IndexByte(pattern, '*')
		nextLit := pattern
		if nextStar >= 0 {
			nextLit = pattern[:nextStar]
		}
		i := strings.Index(s, nextLit)
		if i < 0 {
			return false
		}
		s = s[i:]
	}
}

// Artifact kinds. A test artifact is an output of a test action (test.log,
// test.xml, undeclared outputs); a build artifact is a file in a completed
// target's output group — the wheel, the .skill, an image's .json.sha256.
const (
	kindTest  = "test"
	kindBuild = "build"
)

// artifactKind, artifactShard and artifactFailed are the --kind, --shard and --failed filters. Package-level
// to match jsonOutput, so every artifact subcommand and `target log` honour them. A kind of "" means both,
// and a shard of 0 any.
var (
	artifactKind   string
	artifactShard  int
	artifactFailed bool
)

// addResultFlags registers --shard and --failed on cmd and its subcommands, which narrow test artifacts to
// particular results of their target.
func addResultFlags(cmd *cobra.Command) {
	flags := cmd.PersistentFlags()
	flags.IntVar(&artifactShard, "shard", 0,
		"only test artifacts of this shard, numbered from 1 as in bazel-testlogs/.../shard_N_of_M (default: any)")
	flags.BoolVar(&artifactFailed, "failed", false,
		"only test artifacts of results that did not pass: the failing shard, run or attempt")
}

type artifact struct{ *bbapipb.Artifact }

func (a artifact) label() string {
	if id := a.GetId().GetTestResult(); id != nil {
		return id.GetLabel()
	}
	return a.GetId().GetTargetCompleted().GetLabel()
}

// notFailing are the statuses of a result that went as hoped; any other status, from a failure to a
// timeout to a run that never finished, is one worth reading.
var notFailing = map[bespb.TestStatus]bool{bespb.TestStatus_NO_STATUS: true, bespb.TestStatus_PASSED: true, bespb.TestStatus_FLAKY: true}

// failed reports whether a test artifact came from a result that did not pass.
func (a artifact) failed() bool {
	return a.Kind == kindTest && !notFailing[a.GetStatus()]
}

// result names which result of its target a test artifact came from ("shard 4/6", "run 2/3 attempt 2"), and
// is empty for a build artifact and for a target that ran once.
func (a artifact) result() string {
	var parts []string
	if a.GetTestSummary().GetShardCount() > 1 {
		parts = append(parts, fmt.Sprintf("shard %d/%d", a.GetId().GetTestResult().GetShard(), a.GetTestSummary().GetShardCount()))
	}
	if a.GetTestSummary().GetRunCount() > 1 {
		parts = append(parts, fmt.Sprintf("run %d/%d", a.GetId().GetTestResult().GetRun(), a.GetTestSummary().GetRunCount()))
	}
	if a.GetId().GetTestResult().GetAttempt() > 1 {
		parts = append(parts, fmt.Sprintf("attempt %d", a.GetId().GetTestResult().GetAttempt()))
	}
	return strings.Join(parts, " ")
}

// describe is the artifact as a reader tells it from its siblings: its label, which result of it and how that
// went, and its name.
func (a artifact) describe() string {
	var tags []string
	if r := a.result(); r != "" {
		tags = append(tags, r)
	}
	if a.GetStatus() != bespb.TestStatus_NO_STATUS && a.Kind == kindTest {
		tags = append(tags, a.GetStatus().String())
	}
	s := a.label()
	if len(tags) > 0 {
		s += " (" + strings.Join(tags, ", ") + ")"
	}
	return s + "  " + a.GetFile().GetName()
}

func artifactCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "artifact <invocation-id> [name-substr]",
		Short: "Manage build artifacts",
		Long: `List or fetch build artifacts from a BuildBuddy invocation.

  bbapi artifact <id>              list artifacts
  bbapi artifact <id> <substr>     stream matching artifact to stdout (legacy)

Prefer the explicit subcommands:
  bbapi artifact list <id>
  bbapi artifact cat  <id> <substr>     stream to stdout
  bbapi artifact download <id> <substr> save to file`,
		Args: cobra.RangeArgs(1, 2),
		RunE: func(_ *cobra.Command, args []string) error {
			c, err := newClient()
			if err != nil {
				return err
			}
			defer c.close()
			artifacts, err := listArtifactsResolved(c, args[0])
			if err != nil {
				return err
			}
			if len(args) == 1 {
				return printArtifacts(artifacts)
			}
			return catArtifact(c, artifacts, args[1])
		},
	}
	cmd.PersistentFlags().StringVar(&artifactKind, "kind", "",
		fmt.Sprintf("only %q or %q artifacts (default: both)", kindTest, kindBuild))
	addResultFlags(cmd)
	cmd.AddCommand(artifactListCmd())
	cmd.AddCommand(artifactCatCmd())
	cmd.AddCommand(artifactDownloadCmd())
	return cmd
}

func artifactListCmd() *cobra.Command {
	return &cobra.Command{
		Use:     "list <invocation-id>",
		Aliases: []string{"ls"},
		Short:   "List artifacts for an invocation",
		Args:    cobra.ExactArgs(1),
		RunE: func(_ *cobra.Command, args []string) error {
			c, err := newClient()
			if err != nil {
				return err
			}
			defer c.close()
			artifacts, err := listArtifactsResolved(c, args[0])
			if err != nil {
				return err
			}
			return printArtifacts(artifacts)
		},
	}
}

func artifactCatCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "cat <invocation-id> <name-substr>",
		Short: "Stream artifact content to stdout",
		Args:  cobra.ExactArgs(2),
		RunE: func(_ *cobra.Command, args []string) error {
			c, err := newClient()
			if err != nil {
				return err
			}
			defer c.close()
			artifacts, err := listArtifactsResolved(c, args[0])
			if err != nil {
				return err
			}
			return catArtifact(c, artifacts, args[1])
		},
	}
}

func artifactDownloadCmd() *cobra.Command {
	var output string
	var all bool
	cmd := &cobra.Command{
		Use:   "download <invocation-id> [name-pattern]",
		Short: "Download artifact(s) to file(s)",
		Long: `Download artifact(s) from a BuildBuddy invocation.

Patterns containing '*' use glob matching; otherwise substring match against
"label/name". Without --all, downloads only the first match.

  bbapi artifact download <id> .ambr          # first match containing .ambr
  bbapi artifact download <id> '*.ambr'        # first glob match ending .ambr
  bbapi artifact download <id> --all           # all undeclared test outputs
  bbapi artifact download <id> '*.ambr' --all  # all glob matches`,
		Args: cobra.RangeArgs(1, 2),
		RunE: func(_ *cobra.Command, args []string) error {
			c, err := newClient()
			if err != nil {
				return err
			}
			defer c.close()
			artifacts, err := listArtifactsResolved(c, args[0])
			if err != nil {
				return err
			}
			if all && len(args) < 2 {
				if len(artifacts) == 0 {
					return fmt.Errorf("no artifacts found")
				}
				return downloadAllArtifacts(c, artifacts, output)
			}
			if len(args) < 2 {
				return fmt.Errorf("requires a name-pattern (or use --all with no pattern)")
			}
			if all {
				matches := filterArtifacts(artifacts, args[1])
				if len(matches) == 0 {
					return fmt.Errorf("no artifacts matching %q", args[1])
				}
				return downloadAllArtifacts(c, matches, output)
			}
			return downloadArtifactToFile(c, artifacts, args[1], output)
		},
	}
	cmd.Flags().StringVarP(&output, "output", "o", "", "output directory for --all, or file path for single download")
	cmd.Flags().BoolVar(&all, "all", false, "download all matching artifacts (default: first match only)")
	return cmd
}

func printArtifacts(artifacts []artifact) error {
	if jsonOutput {
		messages := make([]proto.Message, len(artifacts))
		for i, a := range artifacts {
			messages[i] = a.Artifact
		}
		return printProtoJSONArray(messages)
	}
	t := newTable()
	t.header("KIND", "GROUP", "LABEL", "RESULT", "STATUS", "NAME")
	for _, a := range artifacts {
		status := ""
		if a.Kind == kindTest {
			status = a.GetStatus().String()
		}
		t.row(a.Kind, a.OutputGroup, a.label(), a.result(), status, a.GetFile().GetName())
	}
	t.flush()
	return nil
}

// filterKind narrows artifacts to one kind. An empty kind keeps everything; an
// unrecognised one is a user error rather than an empty result.
func filterKind(artifacts []artifact, kind string) ([]artifact, error) {
	switch kind {
	case "":
		return artifacts, nil
	case kindTest, kindBuild:
	default:
		return nil, fmt.Errorf("unknown artifact kind %q: want %q or %q", kind, kindTest, kindBuild)
	}
	var kept []artifact
	for _, a := range artifacts {
		if a.Kind == kind {
			kept = append(kept, a)
		}
	}
	return kept, nil
}

// filterResults narrows to test artifacts of the shard and of the results that did not pass, as asked. With
// neither, everything stays, build artifacts too.
func filterResults(artifacts []artifact, shard int, failedOnly bool) []artifact {
	if shard == 0 && !failedOnly {
		return artifacts
	}
	var kept []artifact
	for _, a := range artifacts {
		if a.Kind == kindTest && (shard == 0 || int(a.GetId().GetTestResult().GetShard()) == shard) && (!failedOnly || a.failed()) {
			kept = append(kept, a)
		}
	}
	return kept
}

// matchKey returns the string matched against: "label/name".
func (a artifact) matchKey() string {
	return a.label() + "/" + a.GetFile().GetName()
}

// resolveArtifact finds a single artifact matching pattern (glob if it
// contains '*', otherwise substring). Returns the first match.
func resolveArtifact(artifacts []artifact, pattern string) (artifact, error) {
	matches := filterArtifacts(artifacts, pattern)
	if len(matches) == 0 {
		seen := map[string]bool{}
		count := 0
		fmt.Fprintf(os.Stderr, "No artifacts matching %q\n", pattern)
		if len(artifacts) > 0 {
			fmt.Fprintf(os.Stderr, "\nAvailable labels (first 5):\n")
			for _, a := range artifacts {
				if !seen[a.label()] {
					seen[a.label()] = true
					fmt.Fprintf(os.Stderr, "  %s\n", a.label())
					count++
					if count >= 5 {
						remaining := 0
						for _, a2 := range artifacts {
							if !seen[a2.label()] {
								seen[a2.label()] = true
								remaining++
							}
						}
						if remaining > 0 {
							fmt.Fprintf(os.Stderr, "  ... (%d more labels)\n", remaining)
						}
						break
					}
				}
			}
			fmt.Fprintf(os.Stderr, "\nHint: match is against \"label/name\" (e.g., \"test_handlers/test.log\")\n")
			if near := nearArtifacts(artifacts, pattern, 5); len(near) > 0 {
				fmt.Fprintf(os.Stderr, "Did you mean (treating '-' and '_' alike):\n")
				for _, a := range near {
					fmt.Fprintf(os.Stderr, "  %s\n", a.matchKey())
				}
			} else {
				fmt.Fprintf(os.Stderr, "Hint: `bbapi artifact list <invocation-id>` shows the exact names\n")
			}
		}
		return artifact{}, fmt.Errorf("no artifacts matching %q", pattern)
	}
	if len(matches) > 1 {
		fmt.Fprintf(os.Stderr, "Multiple matches for %q:\n", pattern)
		for _, a := range matches {
			fmt.Fprintf(os.Stderr, "  %s\n", a.describe())
		}
		fmt.Fprintf(os.Stderr, "Using first match: %s\n(--shard N, --failed or a more specific pattern picks another)\n",
			matches[0].describe())
	}
	return matches[0], nil
}

// nearArtifacts returns up to limit artifacts that pattern would match if '-' and '_' were the same
// character: the spelling of a name guessed from a scenario or a target, where its output uses the other.
func nearArtifacts(artifacts []artifact, pattern string, limit int) []artifact {
	if strings.Contains(pattern, "*") {
		return nil
	}
	alike := strings.NewReplacer("-", "_")
	want := alike.Replace(pattern)
	var near []artifact
	for _, a := range artifacts {
		if strings.Contains(alike.Replace(a.matchKey()), want) {
			near = append(near, a)
			if len(near) == limit {
				break
			}
		}
	}
	return near
}

// filterArtifacts returns all artifacts matching pattern (glob if it contains
// '*', otherwise substring match against "label/name").
func filterArtifacts(artifacts []artifact, pattern string) []artifact {
	var matches []artifact
	if strings.Contains(pattern, "*") {
		for _, a := range artifacts {
			if matchGlob(pattern, a.matchKey()) {
				matches = append(matches, a)
			}
		}
	} else {
		for _, a := range artifacts {
			if strings.Contains(a.matchKey(), pattern) {
				matches = append(matches, a)
			}
		}
	}
	return matches
}

func catArtifact(c *client, artifacts []artifact, substr string) error {
	match, err := resolveArtifact(artifacts, substr)
	if err != nil {
		return err
	}
	return printArtifact(c, match)
}

// printArtifact streams one artifact's content to stdout.
func printArtifact(c *client, match artifact) error {
	downloadURL := c.bytestreamDownloadURL(match.InvocationId, match.GetFile().GetUri())
	data, err := c.fetchURL(downloadURL)
	if err != nil {
		return err
	}
	_, err = os.Stdout.Write(data)
	return err
}

func downloadArtifactToFile(c *client, artifacts []artifact, pattern string, output string) error {
	match, err := resolveArtifact(artifacts, pattern)
	if err != nil {
		return err
	}
	if output == "" {
		output = filepath.Base(match.GetFile().GetName())
	}
	return saveArtifact(c, match, output)
}

func downloadAllArtifacts(c *client, artifacts []artifact, dir string) error {
	if dir == "" {
		dir = "."
	}
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return fmt.Errorf("create output directory: %w", err)
	}
	names := downloadNames(artifacts)
	for i, a := range artifacts {
		dest := filepath.Join(dir, names[i])
		if err := saveArtifact(c, a, dest); err != nil {
			fmt.Fprintf(os.Stderr, "Failed to download %s: %v\n", a.GetFile().GetName(), err)
		}
	}
	return nil
}

// downloadNames gives each artifact the file name it is saved under when they all go to one directory. An
// artifact keeps its base name unless another shares it, as the test.log of every shard of a target does, and
// then it is named for where it came from, so that none overwrites another.
func downloadNames(artifacts []artifact) []string {
	counts := map[string]int{}
	for _, a := range artifacts {
		counts[filepath.Base(a.GetFile().GetName())]++
	}
	unsafe := regexp.MustCompile(`[^A-Za-z0-9._-]+`)
	names := make([]string, len(artifacts))
	used := map[string]bool{}
	for i, a := range artifacts {
		name := filepath.Base(a.GetFile().GetName())
		if counts[name] > 1 {
			origin := []string{strings.Trim(unsafe.ReplaceAllString(a.label(), "_"), "_")}
			if a.GetTestSummary().GetShardCount() > 1 {
				origin = append(origin, fmt.Sprintf("shard_%d_of_%d", a.GetId().GetTestResult().GetShard(), a.GetTestSummary().GetShardCount()))
			}
			if a.GetTestSummary().GetRunCount() > 1 {
				origin = append(origin, fmt.Sprintf("run_%d_of_%d", a.GetId().GetTestResult().GetRun(), a.GetTestSummary().GetRunCount()))
			}
			if a.GetId().GetTestResult().GetAttempt() > 1 {
				origin = append(origin, fmt.Sprintf("attempt_%d", a.GetId().GetTestResult().GetAttempt()))
			}
			name = strings.Join(origin, "__") + "__" + name
		}
		// Whatever else still shares a name, such as one target's outputs of two configurations.
		unique := name
		for n := 2; used[unique]; n++ {
			unique = fmt.Sprintf("%s.%d", name, n)
		}
		used[unique] = true
		names[i] = unique
	}
	return names
}

func saveArtifact(c *client, a artifact, dest string) error {
	downloadURL := c.bytestreamDownloadURL(a.InvocationId, a.GetFile().GetUri())
	data, err := c.fetchURL(downloadURL)
	if err != nil {
		return err
	}
	if err := os.WriteFile(dest, data, 0o644); err != nil {
		return err
	}
	fmt.Fprintf(os.Stderr, "Downloaded to %s (%d bytes)\n", dest, len(data))
	return nil
}

// listArtifactsResolved lists artifacts, auto-resolving workflow invocations to children.
func listArtifactsResolved(c *client, invocationID string) ([]artifact, error) {
	ids, err := resolveInvocationIDs(c, invocationID)
	if err != nil {
		return nil, err
	}
	all := make([]artifact, 0)
	for _, id := range ids {
		arts, err := listArtifacts(c, id)
		if err != nil {
			return nil, fmt.Errorf("list artifacts for %s: %w", id, err)
		}
		all = append(all, arts...)
	}
	kept, err := filterKind(all, artifactKind)
	if err != nil {
		return nil, err
	}
	return filterResults(kept, artifactShard, artifactFailed), nil
}

func listArtifacts(c *client, invocationID string) ([]artifact, error) {
	data, err := readInvocationEvents(c, invocationID)
	if err != nil {
		return nil, fmt.Errorf("fetch BES event stream: %w", err)
	}
	artifacts, err := parseArtifacts(data)
	if err != nil {
		return nil, err
	}
	for i := range artifacts {
		artifacts[i].InvocationId = invocationID
	}
	return artifacts, nil
}

// parseArtifacts extracts both test and build artifacts from a raw BES stream.
//
// Build outputs are reached indirectly: a TargetComplete event names output
// groups, each group names NamedSetOfFiles ids, and those sets hold the files —
// and may reference further sets, since Bazel shares subsets between targets
// rather than repeating them. So the sets are indexed first, then walked
// transitively per target.
func parseArtifacts(data []byte) ([]artifact, error) {
	var rawEvents []json.RawMessage
	if err := json.Unmarshal(data, &rawEvents); err != nil {
		return nil, fmt.Errorf("parse BES event stream: %w", err)
	}
	events := make([]*bespb.BuildEvent, 0, len(rawEvents))
	fileSets := map[string]*bespb.NamedSetOfFiles{}
	// How many shards and runs each test had, which only its summary, last in the stream, says.
	type summaryKey struct{ label, configuration string }
	summaries := map[summaryKey]*bespb.TestSummary{}
	for _, raw := range rawEvents {
		var ev bespb.BuildEvent
		if err := unmarshalJSON(raw, &ev); err != nil {
			return nil, fmt.Errorf("parse BES event: %w", err)
		}
		events = append(events, &ev)
		if ns := ev.GetNamedSetOfFiles(); ns != nil {
			fileSets[ev.GetId().GetNamedSet().GetId()] = ns
		}
		if ts := ev.GetTestSummary(); ts != nil {
			id := ev.GetId().GetTestSummary()
			summaries[summaryKey{id.GetLabel(), id.GetConfiguration().GetId()}] = ts
		}
	}

	var result []artifact
	seen := map[string]bool{}
	add := func(a artifact) error {
		encoded, err := proto.MarshalOptions{Deterministic: true}.Marshal(a.Artifact)
		if err != nil {
			return err
		}
		key := string(encoded)
		if !seen[key] {
			seen[key] = true
			result = append(result, a)
		}
		return nil
	}
	for _, ev := range events {
		if tr := ev.GetTestResult(); tr != nil {
			id := ev.GetId().GetTestResult()
			label := id.GetLabel()
			for _, f := range tr.GetTestActionOutput() {
				if err := add(artifact{&bbapipb.Artifact{
					Id: ev.GetId(), File: f, Kind: kindTest,
					TestSummary: summaries[summaryKey{label, id.GetConfiguration().GetId()}], Status: tr.GetStatus(),
				}}); err != nil {
					return nil, fmt.Errorf("index artifact: %w", err)
				}
			}
			continue
		}
		completed := ev.GetCompleted()
		if completed == nil {
			continue
		}
		for _, group := range completed.GetOutputGroup() {
			for _, f := range filesInSets(fileSets, group.GetFileSets()) {
				if err := add(artifact{&bbapipb.Artifact{
					Id: ev.GetId(), File: f, Kind: kindBuild, OutputGroup: group.GetName(),
				}}); err != nil {
					return nil, fmt.Errorf("index artifact: %w", err)
				}
			}
		}
	}
	return result, nil
}

// filesInSets flattens the named file sets reachable from ids. Sets form a DAG
// that a large build shares aggressively, so visited guards against walking the
// same subtree once per referring target (and against a malformed cycle).
func filesInSets(byID map[string]*bespb.NamedSetOfFiles, ids []*bespb.BuildEventId_NamedSetOfFilesId) []*bespb.File {
	var files []*bespb.File
	visited := map[string]bool{}
	var walk func(id string)
	walk = func(id string) {
		if id == "" || visited[id] {
			return
		}
		visited[id] = true
		set := byID[id]
		if set == nil {
			return
		}
		files = append(files, set.GetFiles()...)
		for _, child := range set.GetFileSets() {
			walk(child.GetId())
		}
	}
	for _, id := range ids {
		walk(id.GetId())
	}
	return files
}

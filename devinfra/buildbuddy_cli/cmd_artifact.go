package main

import (
	"encoding/json"
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"strings"

	bespb "github.com/buildbuddy-io/buildbuddy/proto/build_event_stream"
	"github.com/spf13/cobra"
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

// artifactKind is the --kind filter; empty means both. Package-level to match
// jsonOutput, so every artifact subcommand honours it.
var artifactKind string

type artifact struct {
	Label string `json:"label"`
	Name  string `json:"name"`
	URI   string `json:"uri"`
	Kind  string `json:"kind"`
	// Bazel output group, for build artifacts only. Aspects contribute their own
	// groups (this repo adds rules_lint_report, mypy, clippy_checks,
	// rustfmt_checks), so the group is what separates a target's real outputs
	// from its lint reports.
	OutputGroup string `json:"outputGroup,omitempty"`
	// Directory prefix BES reports separately from Name, e.g.
	// "bazel-out/k8-fastbuild/bin". Name alone is ambiguous: a source file has an
	// empty prefix while the generated file of the same name sits under bazel-out,
	// and a configuration transition puts its outputs under a distinct prefix
	// ("bazel-out/k8-fastbuild-ST-<hash>/bin"). Only Prefix+Name identifies a file.
	PathPrefix string `json:"pathPrefix,omitempty"`
	// Content digest and size as BES reports them. For a single-file release the
	// digest is the published content identity, so callers can compare against a
	// registry or release tag without fetching the bytes at all.
	Digest string `json:"digest,omitempty"`
	Size   int64  `json:"size,omitempty"`
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
		b, err := json.MarshalIndent(artifacts, "", "  ")
		if err != nil {
			return err
		}
		os.Stdout.Write(b)
		fmt.Println()
		return nil
	}
	t := newTable()
	t.header("KIND", "GROUP", "LABEL", "NAME")
	for _, a := range artifacts {
		t.row(a.Kind, a.OutputGroup, a.Label, a.Name)
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

// matchKey returns the string matched against: "label/name".
func (a artifact) matchKey() string {
	return a.Label + "/" + a.Name
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
				if !seen[a.Label] {
					seen[a.Label] = true
					fmt.Fprintf(os.Stderr, "  %s\n", a.Label)
					count++
					if count >= 5 {
						remaining := 0
						for _, a2 := range artifacts {
							if !seen[a2.Label] {
								seen[a2.Label] = true
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
		}
		return artifact{}, fmt.Errorf("no artifacts matching %q", pattern)
	}
	if len(matches) > 1 {
		fmt.Fprintf(os.Stderr, "Multiple matches for %q:\n", pattern)
		for _, a := range matches {
			fmt.Fprintf(os.Stderr, "  %s  %s\n", a.Label, a.Name)
		}
		fmt.Fprintf(os.Stderr, "Using first match: %s %s\n", matches[0].Label, matches[0].Name)
	}
	return matches[0], nil
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
	downloadURL := fmt.Sprintf("%s/file/download?bytestream_url=%s",
		c.baseURL, url.QueryEscape(match.URI))
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
		output = filepath.Base(match.Name)
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
	for _, a := range artifacts {
		dest := filepath.Join(dir, filepath.Base(a.Name))
		if err := saveArtifact(c, a, dest); err != nil {
			fmt.Fprintf(os.Stderr, "Failed to download %s: %v\n", a.Name, err)
		}
	}
	return nil
}

func saveArtifact(c *client, a artifact, dest string) error {
	downloadURL := fmt.Sprintf("%s/file/download?bytestream_url=%s",
		c.baseURL, url.QueryEscape(a.URI))
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
	return filterKind(all, artifactKind)
}

func listArtifacts(c *client, invocationID string) ([]artifact, error) {
	besURL := fmt.Sprintf("%s/file/download?invocation_id=%s&artifact=raw_json",
		c.baseURL, url.QueryEscape(invocationID))
	data, err := c.fetchURL(besURL)
	if err != nil {
		return nil, fmt.Errorf("fetch BES event stream: %w", err)
	}
	return parseArtifacts(data)
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
	for _, raw := range rawEvents {
		var ev bespb.BuildEvent
		if err := unmarshalJSON(raw, &ev); err != nil {
			return nil, fmt.Errorf("parse BES event: %w", err)
		}
		events = append(events, &ev)
		if ns := ev.GetNamedSetOfFiles(); ns != nil {
			fileSets[ev.GetId().GetNamedSet().GetId()] = ns
		}
	}

	var result []artifact
	seen := map[artifact]bool{}
	add := func(a artifact) {
		if !seen[a] {
			seen[a] = true
			result = append(result, a)
		}
	}
	for _, ev := range events {
		if tr := ev.GetTestResult(); tr != nil {
			label := ev.GetId().GetTestResult().GetLabel()
			for _, f := range tr.GetTestActionOutput() {
				add(artifact{
					Label:      label,
					Name:       f.GetName(),
					URI:        f.GetUri(),
					Kind:       kindTest,
					PathPrefix: strings.Join(f.GetPathPrefix(), "/"),
					Digest:     f.GetDigest(),
					Size:       f.GetLength(),
				})
			}
			continue
		}
		completed := ev.GetCompleted()
		if completed == nil {
			continue
		}
		label := ev.GetId().GetTargetCompleted().GetLabel()
		for _, group := range completed.GetOutputGroup() {
			for _, f := range filesInSets(fileSets, group.GetFileSets()) {
				add(artifact{
					Label:       label,
					Name:        f.GetName(),
					URI:         f.GetUri(),
					Kind:        kindBuild,
					OutputGroup: group.GetName(),
					PathPrefix:  strings.Join(f.GetPathPrefix(), "/"),
					Digest:      f.GetDigest(),
					Size:        f.GetLength(),
				})
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

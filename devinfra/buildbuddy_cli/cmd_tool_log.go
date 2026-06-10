package main

import (
	"encoding/json"
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"

	bespb "github.com/buildbuddy-io/buildbuddy/proto/build_event_stream"
	"github.com/spf13/cobra"
	"google.golang.org/protobuf/encoding/protojson"
)

type toolLog struct {
	InvocationID string `json:"invocation_id"`
	Name         string `json:"name"`
	URI          string `json:"uri"`
	Source       string `json:"source"`
	SizeBytes    int    `json:"size_bytes,omitempty"`
	contents     []byte
}

func toolLogCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "tool-log <invocation-id>",
		Short: "Manage invocation-level BES build tool logs",
		Long: `Manage Build Event Stream build tool logs for an invocation.

Bazel publishes files such as command.profile.gz as build tool logs, not as
test artifacts. Patterns containing '*' use glob matching; otherwise substring
match against "invocation-id/name".`,
		Args: cobra.ExactArgs(1),
		RunE: func(_ *cobra.Command, args []string) error {
			c, err := newClient()
			if err != nil {
				return err
			}
			logs, err := listToolLogsResolved(c, args[0])
			if err != nil {
				return err
			}
			return printToolLogs(logs)
		},
	}
	cmd.AddCommand(toolLogListCmd())
	cmd.AddCommand(toolLogCatCmd())
	cmd.AddCommand(toolLogDownloadCmd())
	return cmd
}

func toolLogListCmd() *cobra.Command {
	return &cobra.Command{
		Use:     "list <invocation-id>",
		Aliases: []string{"ls"},
		Short:   "List build tool logs for an invocation",
		Args:    cobra.ExactArgs(1),
		RunE: func(_ *cobra.Command, args []string) error {
			c, err := newClient()
			if err != nil {
				return err
			}
			logs, err := listToolLogsResolved(c, args[0])
			if err != nil {
				return err
			}
			return printToolLogs(logs)
		},
	}
}

func toolLogCatCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "cat <invocation-id> <name-pattern>",
		Short: "Stream a build tool log to stdout",
		Args:  cobra.ExactArgs(2),
		RunE: func(_ *cobra.Command, args []string) error {
			c, err := newClient()
			if err != nil {
				return err
			}
			logs, err := listToolLogsResolved(c, args[0])
			if err != nil {
				return err
			}
			match, err := resolveToolLog(logs, args[1])
			if err != nil {
				return err
			}
			data, err := readToolLog(c, match)
			if err != nil {
				return err
			}
			_, err = os.Stdout.Write(data)
			return err
		},
	}
}

func toolLogDownloadCmd() *cobra.Command {
	var output string
	var all bool
	cmd := &cobra.Command{
		Use:   "download <invocation-id> [name-pattern]",
		Short: "Download build tool log(s)",
		Long: `Download Build Event Stream build tool logs.

With --all and multiple child invocations, output filenames are prefixed with
the invocation ID to avoid collisions.

  bbapi tool-log download <id> command.profile.gz
  bbapi tool-log download <runner-id> command.profile.gz --all -o profiles`,
		Args: cobra.RangeArgs(1, 2),
		RunE: func(_ *cobra.Command, args []string) error {
			c, err := newClient()
			if err != nil {
				return err
			}
			logs, err := listToolLogsResolved(c, args[0])
			if err != nil {
				return err
			}
			if all {
				matches := logs
				if len(args) == 2 {
					matches = filterToolLogs(logs, args[1])
				}
				if len(matches) == 0 {
					if len(args) == 2 {
						return fmt.Errorf("no tool logs matching %q", args[1])
					}
					return fmt.Errorf("no tool logs found")
				}
				return downloadAllToolLogs(c, matches, output)
			}
			if len(args) != 2 {
				return fmt.Errorf("requires a name-pattern (or use --all with no pattern)")
			}
			match, err := resolveToolLog(logs, args[1])
			if err != nil {
				return err
			}
			if output == "" {
				output = filepath.Base(match.Name)
			}
			return saveToolLog(c, match, output)
		},
	}
	cmd.Flags().StringVarP(&output, "output", "o", "", "output directory for --all, or file path for single download")
	cmd.Flags().BoolVar(&all, "all", false, "download all matching logs (default: first match only)")
	return cmd
}

func printToolLogs(logs []toolLog) error {
	if jsonOutput {
		b, err := json.MarshalIndent(logs, "", "  ")
		if err != nil {
			return err
		}
		os.Stdout.Write(b)
		fmt.Println()
		return nil
	}
	t := newTable()
	t.header("INVOCATION", "NAME", "SOURCE", "SIZE")
	for _, l := range logs {
		size := ""
		if l.SizeBytes > 0 {
			size = fmt.Sprintf("%d", l.SizeBytes)
		}
		t.row(l.InvocationID, l.Name, l.Source, size)
	}
	t.flush()
	return nil
}

func (l toolLog) matchKey() string {
	return l.InvocationID + "/" + l.Name
}

func resolveToolLog(logs []toolLog, pattern string) (toolLog, error) {
	matches := filterToolLogs(logs, pattern)
	if len(matches) == 0 {
		fmt.Fprintf(os.Stderr, "No build tool logs matching %q\n", pattern)
		if len(logs) > 0 {
			fmt.Fprintf(os.Stderr, "\nAvailable tool logs:\n")
			for _, l := range logs {
				fmt.Fprintf(os.Stderr, "  %s  %s\n", l.InvocationID, l.Name)
			}
		}
		return toolLog{}, fmt.Errorf("no build tool logs matching %q", pattern)
	}
	if len(matches) > 1 {
		fmt.Fprintf(os.Stderr, "Multiple build tool logs match %q:\n", pattern)
		for _, l := range matches {
			fmt.Fprintf(os.Stderr, "  %s  %s\n", l.InvocationID, l.Name)
		}
		fmt.Fprintf(os.Stderr, "Using first match: %s %s\n", matches[0].InvocationID, matches[0].Name)
	}
	return matches[0], nil
}

func filterToolLogs(logs []toolLog, pattern string) []toolLog {
	var matches []toolLog
	if strings.Contains(pattern, "*") {
		for _, l := range logs {
			if matchGlob(pattern, l.matchKey()) || matchGlob(pattern, l.Name) {
				matches = append(matches, l)
			}
		}
		return matches
	}
	for _, l := range logs {
		if strings.Contains(l.matchKey(), pattern) || strings.Contains(l.Name, pattern) {
			matches = append(matches, l)
		}
	}
	return matches
}

func listToolLogsResolved(c *client, invocationID string) ([]toolLog, error) {
	ids, err := resolveInvocationIDs(c, invocationID)
	if err != nil {
		return nil, err
	}
	var all []toolLog
	for _, id := range ids {
		logs, err := listToolLogs(c, id)
		if err != nil {
			return nil, fmt.Errorf("list tool logs for %s: %w", id, err)
		}
		all = append(all, logs...)
	}
	return all, nil
}

func listToolLogs(c *client, invocationID string) ([]toolLog, error) {
	besURL := fmt.Sprintf("%s/file/download?invocation_id=%s&artifact=raw_json",
		c.baseURL, url.QueryEscape(invocationID))
	data, err := c.fetchURL(besURL)
	if err != nil {
		return nil, fmt.Errorf("fetch BES event stream: %w", err)
	}
	var rawEvents []json.RawMessage
	if err := json.Unmarshal(data, &rawEvents); err != nil {
		return nil, fmt.Errorf("parse BES event stream: %w", err)
	}
	var result []toolLog
	for _, raw := range rawEvents {
		var ev bespb.BuildEvent
		if err := protojson.Unmarshal(raw, &ev); err != nil {
			return nil, fmt.Errorf("parse BES event: %w", err)
		}
		logs := ev.GetBuildToolLogs()
		if logs == nil {
			continue
		}
		for _, f := range logs.GetLog() {
			contents := f.GetContents()
			source, size := toolLogSourceAndSize(f.GetUri(), contents)
			result = append(result, toolLog{
				InvocationID: invocationID,
				Name:         f.GetName(),
				URI:          f.GetUri(),
				Source:       source,
				SizeBytes:    size,
				contents:     contents,
			})
		}
	}
	return result, nil
}

func downloadAllToolLogs(c *client, logs []toolLog, dir string) error {
	if dir == "" {
		dir = "."
	}
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return fmt.Errorf("create output directory: %w", err)
	}
	var failures []string
	for _, l := range logs {
		dest := filepath.Join(dir, l.InvocationID+"-"+filepath.Base(l.Name))
		if err := saveToolLog(c, l, dest); err != nil {
			fmt.Fprintf(os.Stderr, "Failed to download %s %s: %v\n", l.InvocationID, l.Name, err)
			failures = append(failures, l.InvocationID+"/"+l.Name)
		}
	}
	if len(failures) > 0 {
		return fmt.Errorf("failed to download %d build tool log(s): %s", len(failures), strings.Join(failures, ", "))
	}
	return nil
}

func saveToolLog(c *client, l toolLog, dest string) error {
	data, err := readToolLog(c, l)
	if err != nil {
		return err
	}
	if err := os.WriteFile(dest, data, 0o644); err != nil {
		return err
	}
	fmt.Fprintf(os.Stderr, "Downloaded to %s (%d bytes)\n", dest, len(data))
	return nil
}

func readToolLog(c *client, l toolLog) ([]byte, error) {
	if l.URI != "" {
		return fetchBytestream(c, l.URI)
	}
	if len(l.contents) > 0 {
		return l.contents, nil
	}
	return nil, fmt.Errorf("build tool log %q has neither URI nor inline contents", l.Name)
}

func fetchBytestream(c *client, uri string) ([]byte, error) {
	downloadURL := fmt.Sprintf("%s/file/download?bytestream_url=%s", c.baseURL, url.QueryEscape(uri))
	return c.fetchURL(downloadURL)
}

func toolLogSourceAndSize(uri string, contents []byte) (string, int) {
	if uri != "" {
		return "bytestream", bytestreamSize(uri)
	}
	if len(contents) > 0 {
		return "inline", len(contents)
	}
	return "empty", 0
}

func bytestreamSize(uri string) int {
	i := strings.LastIndex(uri, "/")
	if i == -1 || i == len(uri)-1 {
		return 0
	}
	size, err := strconv.Atoi(uri[i+1:])
	if err != nil {
		return 0
	}
	return size
}

package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"

	bbapipb "github.com/agentydragon/ducktape/devinfra/buildbuddy/bbapi/proto"
	bespb "github.com/buildbuddy-io/buildbuddy/proto/build_event_stream"
	"github.com/spf13/cobra"
	"google.golang.org/protobuf/proto"
)

type toolLog struct{ *bbapipb.ToolLog }

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
			defer c.close()
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
			defer c.close()
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
			defer c.close()
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
			defer c.close()
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
				output = filepath.Base(match.GetFile().GetName())
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
		messages := make([]proto.Message, len(logs))
		for i, l := range logs {
			messages[i] = l.ToolLog
		}
		return printProtoJSONArray(messages)
	}
	t := newTable()
	t.header("INVOCATION", "NAME", "SOURCE", "SIZE")
	for _, l := range logs {
		source, n := toolLogSourceAndSize(l.GetFile().GetUri(), l.GetFile().GetContents())
		size := ""
		if n > 0 {
			size = fmt.Sprintf("%d", n)
		}
		t.row(l.InvocationId, l.GetFile().GetName(), source, size)
	}
	t.flush()
	return nil
}

func (l toolLog) matchKey() string {
	return l.InvocationId + "/" + l.GetFile().GetName()
}

func resolveToolLog(logs []toolLog, pattern string) (toolLog, error) {
	matches := filterToolLogs(logs, pattern)
	if len(matches) == 0 {
		fmt.Fprintf(os.Stderr, "No build tool logs matching %q\n", pattern)
		if len(logs) > 0 {
			fmt.Fprintf(os.Stderr, "\nAvailable tool logs:\n")
			for _, l := range logs {
				fmt.Fprintf(os.Stderr, "  %s  %s\n", l.InvocationId, l.GetFile().GetName())
			}
		}
		return toolLog{}, fmt.Errorf("no build tool logs matching %q", pattern)
	}
	if len(matches) > 1 {
		fmt.Fprintf(os.Stderr, "Multiple build tool logs match %q:\n", pattern)
		for _, l := range matches {
			fmt.Fprintf(os.Stderr, "  %s  %s\n", l.InvocationId, l.GetFile().GetName())
		}
		fmt.Fprintf(os.Stderr, "Using first match: %s %s\n", matches[0].InvocationId, matches[0].GetFile().GetName())
	}
	return matches[0], nil
}

func filterToolLogs(logs []toolLog, pattern string) []toolLog {
	var matches []toolLog
	if strings.Contains(pattern, "*") {
		for _, l := range logs {
			if matchGlob(pattern, l.matchKey()) || matchGlob(pattern, l.GetFile().GetName()) {
				matches = append(matches, l)
			}
		}
		return matches
	}
	for _, l := range logs {
		if strings.Contains(l.matchKey(), pattern) || strings.Contains(l.GetFile().GetName(), pattern) {
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
	data, err := readInvocationEvents(c, invocationID)
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
		if err := unmarshalJSON(raw, &ev); err != nil {
			return nil, fmt.Errorf("parse BES event: %w", err)
		}
		logs := ev.GetBuildToolLogs()
		if logs == nil {
			continue
		}
		for _, f := range logs.GetLog() {
			result = append(result, toolLog{&bbapipb.ToolLog{InvocationId: invocationID, File: f}})
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
		dest := filepath.Join(dir, l.InvocationId+"-"+filepath.Base(l.GetFile().GetName()))
		if err := saveToolLog(c, l, dest); err != nil {
			fmt.Fprintf(os.Stderr, "Failed to download %s %s: %v\n", l.InvocationId, l.GetFile().GetName(), err)
			failures = append(failures, l.InvocationId+"/"+l.GetFile().GetName())
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
	if l.GetFile().GetUri() != "" {
		return fetchBytestream(c, l.InvocationId, l.GetFile().GetUri())
	}
	if contents, ok := l.GetFile().GetFile().(*bespb.File_Contents); ok {
		return contents.Contents, nil
	}
	return nil, fmt.Errorf("build tool log %q has neither URI nor inline contents", l.GetFile().GetName())
}

func fetchBytestream(c *client, invocationID, uri string) ([]byte, error) {
	return c.fetchURL(c.bytestreamDownloadURL(invocationID, uri))
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

package main

import (
	"encoding/json"
	"fmt"
	"os"

	bbapipb "github.com/agentydragon/ducktape/devinfra/buildbuddy/bbapi/proto"
	bespb "github.com/buildbuddy-io/buildbuddy/proto/build_event_stream"
	"github.com/spf13/cobra"
	"google.golang.org/protobuf/encoding/protojson"
)

// Only these fields need raw JSON: protobuf scalars and repeated fields cannot
// distinguish an absent count/command line from one explicitly reported empty.
type invocationPresenceEvent struct {
	TestSummary *struct {
		AttemptCount json.RawMessage `json:"attemptCount"`
		CachedCount  json.RawMessage `json:"totalNumCached"`
	} `json:"testSummary"`
	OptionsParsed *struct {
		ExplicitCmdLine json.RawMessage `json:"explicitCmdLine"`
	} `json:"optionsParsed"`
}

func invocationExportCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "export <invocation-id>",
		Short: "Export a finished invocation as structured JSON",
		Args:  cobra.ExactArgs(1),
		RunE: func(_ *cobra.Command, args []string) error {
			c, err := newClient()
			if err != nil {
				return err
			}
			defer c.close()
			raw, err := readInvocationEvents(c, args[0])
			if err != nil {
				return err
			}
			exported, err := parseInvocationExport(raw)
			if err != nil {
				return err
			}
			return writeInvocationExport(exported)
		},
	}
}

// writeInvocationExport emits JSON regardless of the global --json flag.
func writeInvocationExport(exported *bbapipb.InvocationExport) error {
	data, err := (protojson.MarshalOptions{Indent: "  ", EmitUnpopulated: true}).Marshal(exported)
	if err != nil {
		return err
	}
	_, err = fmt.Fprintln(os.Stdout, string(data))
	return err
}

// parseInvocationExport parses a raw BuildBuddy BES stream into the stable CI
// contract. Presence flags distinguish an absent count from explicit zero.
func parseInvocationExport(raw []byte) (*bbapipb.InvocationExport, error) {
	var rawEvents []json.RawMessage
	if err := json.Unmarshal(raw, &rawEvents); err != nil {
		return nil, fmt.Errorf("parse invocation event stream: %w", err)
	}
	if rawEvents == nil {
		return nil, fmt.Errorf("invocation event stream is not a list")
	}

	artifacts, err := parseArtifacts(raw)
	if err != nil {
		return nil, err
	}
	result := &bbapipb.InvocationExport{TestStatus: map[string]bespb.TestStatus{}}
	for _, artifact := range artifacts {
		if artifact.Kind == kindBuild {
			result.Outputs = append(result.Outputs, artifact.Artifact)
		}
	}

	finishedCount := 0
	successfulCount := 0
	var commands []string
	var explicitCmdLines []*[]string
	var requestedPatterns []string
	var commitSHAs []string
	for i, rawEvent := range rawEvents {
		var event bespb.BuildEvent
		if err := unmarshalJSON(rawEvent, &event); err != nil {
			return nil, fmt.Errorf("parse invocation event %d: %w", i, err)
		}
		var presence invocationPresenceEvent
		if err := json.Unmarshal(rawEvent, &presence); err != nil {
			return nil, fmt.Errorf("parse invocation event presence %d: %w", i, err)
		}

		if summary := event.GetTestSummary(); summary != nil {
			id := event.GetId().GetTestSummary()
			if id == nil || id.GetLabel() == "" {
				return nil, fmt.Errorf("test summary event %d has no target label", i)
			}
			result.TestStatus[id.GetLabel()] = summary.GetOverallStatus()
			record := &bbapipb.TestSummaryRecord{Id: id, Summary: summary}
			if presence.TestSummary != nil {
				record.AttemptCountPresent = jsonPresent(presence.TestSummary.AttemptCount)
				record.CachedCountPresent = jsonPresent(presence.TestSummary.CachedCount)
			}
			result.TestSummaries = append(result.TestSummaries, record)
			continue
		}

		if finished := event.GetFinished(); finished != nil {
			finishedCount++
			if finished.GetExitCode().GetName() == "SUCCESS" {
				successfulCount++
			}
		}
		if started := event.GetStarted(); started != nil {
			commands = append(commands, started.GetCommand())
		}
		if options := event.GetOptionsParsed(); options != nil {
			if presence.OptionsParsed != nil {
				if jsonPresent(presence.OptionsParsed.ExplicitCmdLine) {
					explicit := options.GetExplicitCmdLine()
					explicitCmdLines = append(explicitCmdLines, &explicit)
				} else {
					explicitCmdLines = append(explicitCmdLines, nil)
				}
			} else {
				explicitCmdLines = append(explicitCmdLines, nil)
			}
		}
		if pattern := event.GetId().GetPattern(); pattern != nil {
			requestedPatterns = append(requestedPatterns, pattern.GetPattern()...)
		}
		if status := event.GetWorkspaceStatus(); status != nil {
			for _, item := range status.GetItem() {
				if item.GetKey() == "COMMIT_SHA" {
					commitSHAs = append(commitSHAs, item.GetValue())
				}
			}
		}
	}

	result.Finished = finishedCount == 1
	result.Successful = finishedCount == 1 && successfulCount == 1
	if len(commands) == 1 {
		result.Command = stringPointer(commands[0])
	}
	if len(explicitCmdLines) == 1 && explicitCmdLines[0] != nil {
		result.ExplicitCmdLine = &bbapipb.Arguments{Values: *explicitCmdLines[0]}
	}
	if len(requestedPatterns) > 0 {
		result.RequestedPatterns = &bbapipb.Arguments{Values: requestedPatterns}
	}
	commitValues := map[string]bool{}
	for _, sha := range commitSHAs {
		commitValues[sha] = true
	}
	if len(commitValues) == 1 {
		for sha := range commitValues {
			if sha != "" {
				result.CommitSha = stringPointer(sha)
			}
		}
	}
	return result, nil
}

func jsonPresent(raw json.RawMessage) bool {
	return len(raw) != 0 && string(raw) != "null"
}

func stringPointer(value string) *string { return &value }

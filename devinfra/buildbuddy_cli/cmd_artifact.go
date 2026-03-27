package main

import (
	"encoding/json"
	"fmt"
	"net/url"
	"os"
	"strings"

	bespb "github.com/buildbuddy-io/buildbuddy/proto/build_event_stream"
	"github.com/spf13/cobra"
	"google.golang.org/protobuf/encoding/protojson"
)

type artifact struct {
	Label string `json:"label"`
	Name  string `json:"name"`
	URI   string `json:"uri"`
}

func artifactCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "artifact <invocation-id> [name-substr]",
		Short: "List artifacts, or download one by name match",
		Args:  cobra.RangeArgs(1, 2),
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
			return downloadArtifact(c, artifacts, args[1])
		},
	}
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
	t.header("LABEL", "NAME")
	for _, a := range artifacts {
		t.row(a.Label, a.Name)
	}
	t.flush()
	return nil
}

func downloadArtifact(c *client, artifacts []artifact, substr string) error {
	var matches []artifact
	for _, a := range artifacts {
		if strings.Contains(a.Label+"/"+a.Name, substr) {
			matches = append(matches, a)
		}
	}
	if len(matches) == 0 {
		// Show a few available artifacts as hints
		seen := map[string]bool{}
		count := 0
		fmt.Fprintf(os.Stderr, "No artifacts matching %q\n", substr)
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
		return fmt.Errorf("no artifacts matching %q", substr)
	}
	if len(matches) > 1 {
		fmt.Fprintf(os.Stderr, "Multiple matches for %q:\n", substr)
		for _, a := range matches {
			fmt.Fprintf(os.Stderr, "  %s  %s\n", a.Label, a.Name)
		}
		fmt.Fprintf(os.Stderr, "Using first match: %s %s\n", matches[0].Label, matches[0].Name)
	}
	downloadURL := fmt.Sprintf("%s/file/download?bytestream_url=%s",
		c.baseURL, url.QueryEscape(matches[0].URI))
	data, err := c.fetchURL(downloadURL)
	if err != nil {
		return err
	}
	_, err = os.Stdout.Write(data)
	return err
}

// listArtifactsResolved lists artifacts, auto-resolving workflow invocations to children.
func listArtifactsResolved(c *client, invocationID string) ([]artifact, error) {
	ids, err := resolveInvocationIDs(c, invocationID)
	if err != nil {
		return nil, err
	}
	var all []artifact
	for _, id := range ids {
		arts, err := listArtifacts(c, id)
		if err != nil {
			return nil, fmt.Errorf("list artifacts for %s: %w", id, err)
		}
		all = append(all, arts...)
	}
	return all, nil
}

func listArtifacts(c *client, invocationID string) ([]artifact, error) {
	besURL := fmt.Sprintf("%s/file/download?invocation_id=%s&artifact=raw_json",
		c.baseURL, url.QueryEscape(invocationID))
	data, err := c.fetchURL(besURL)
	if err != nil {
		return nil, fmt.Errorf("fetch BES event stream: %w", err)
	}
	// The raw_json endpoint returns a JSON array of BuildEvent protos.
	// protojson doesn't handle arrays, so decode element-by-element.
	var rawEvents []json.RawMessage
	if err := json.Unmarshal(data, &rawEvents); err != nil {
		return nil, fmt.Errorf("parse BES event stream: %w", err)
	}
	var result []artifact
	for _, raw := range rawEvents {
		var ev bespb.BuildEvent
		if err := protojson.Unmarshal(raw, &ev); err != nil {
			return nil, fmt.Errorf("parse BES event: %w", err)
		}
		tr := ev.GetTestResult()
		if tr == nil {
			continue
		}
		label := ""
		if tid := ev.GetId().GetTestResult(); tid != nil {
			label = tid.GetLabel()
		}
		for _, f := range tr.GetTestActionOutput() {
			result = append(result, artifact{Label: label, Name: f.GetName(), URI: f.GetUri()})
		}
	}
	return result, nil
}

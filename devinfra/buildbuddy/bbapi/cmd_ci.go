package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"os"
	"regexp"
	"sync"

	publicpb "github.com/buildbuddy-io/buildbuddy/proto/api/v1/invocation"
	invocationpb "github.com/buildbuddy-io/buildbuddy/proto/invocation"
	"github.com/spf13/cobra"
)

// The public endpoint distinguishes an unknown invocation (an empty list) from
// a failed request. The internal endpoint reports both as HTTP errors.
func invocationStatusCmd() *cobra.Command {
	return &cobra.Command{Use: "status <invocation-id>", Short: "Get invocation state; an unknown invocation returns an empty list", Args: cobra.ExactArgs(1), RunE: func(_ *cobra.Command, args []string) error {
		c, err := newClient()
		if err != nil {
			return err
		}
		defer c.close()
		resp, err := getInvocationStatus(c, args[0])
		if err != nil {
			return err
		}
		return printProtoJSON(resp)
	}}
}

func getInvocationStatus(c *client, id string) (*publicpb.GetInvocationResponse, error) {
	resp := &publicpb.GetInvocationResponse{}
	err := c.callPath("/api/v1/GetInvocation", &publicpb.GetInvocationRequest{Selector: &publicpb.InvocationSelector{InvocationId: id}}, resp)
	return resp, err
}

func readInvocationEvents(c *client, invocationID string) ([]byte, error) {
	params := url.Values{"invocation_id": {invocationID}, "artifact": {"raw_json"}}
	return c.fetchURL(c.baseURL + "/file/download?" + params.Encode())
}

func invocationEventsCmd() *cobra.Command {
	return &cobra.Command{Use: "events <invocation-id>", Short: "Export the raw JSON build event stream", Args: cobra.ExactArgs(1), RunE: func(_ *cobra.Command, args []string) error {
		c, err := newClient()
		if err != nil {
			return err
		}
		defer c.close()
		body, err := readInvocationEvents(c, args[0])
		if err != nil {
			return err
		}
		_, err = os.Stdout.Write(body)
		return err
	}}
}

func invocationFirstFailureCmd() *cobra.Command {
	return &cobra.Command{Use: "first-failure <invocation-id>", Short: "Get the first failing test currently indexed by BuildBuddy", Args: cobra.ExactArgs(1), RunE: func(_ *cobra.Command, args []string) error {
		c, err := newClient()
		if err != nil {
			return err
		}
		defer c.close()
		label, err := failedTest(c, args[0], "")
		if err != nil {
			return err
		}
		return json.NewEncoder(os.Stdout).Encode(struct {
			Label string `json:"label"`
		}{label})
	}}
}

var invocationIDPattern = regexp.MustCompile(`^[[:xdigit:]]{8}-[[:xdigit:]]{4}-[[:xdigit:]]{4}-[[:xdigit:]]{4}-[[:xdigit:]]{12}$`)

// All requests run concurrently and finish before returning, including when one
// fails. This fits the short cancellation grace period on GitHub runners.
func cancelInvocations(c *client, ids []string) error {
	for _, id := range ids {
		if !invocationIDPattern.MatchString(id) {
			return fmt.Errorf("invalid invocation ID %q", id)
		}
	}
	errs := make([]error, len(ids))
	var wg sync.WaitGroup
	for i, id := range ids {
		wg.Add(1)
		go func(i int, id string) {
			defer wg.Done()
			err := c.call("CancelExecutions", &invocationpb.CancelExecutionsRequest{InvocationId: id}, &invocationpb.CancelExecutionsResponse{})
			if err != nil {
				errs[i] = fmt.Errorf("cancel %s: %w", id, err)
			}
		}(i, id)
	}
	wg.Wait()
	return errors.Join(errs...)
}

func invocationCancelCmd() *cobra.Command {
	return &cobra.Command{Use: "cancel <invocation-id>...", Short: "Cancel only the supplied invocations, concurrently", Args: cobra.MinimumNArgs(1), RunE: func(_ *cobra.Command, args []string) error {
		c, err := newClient()
		if err != nil {
			return err
		}
		defer c.close()
		return cancelInvocations(c, args)
	}}
}

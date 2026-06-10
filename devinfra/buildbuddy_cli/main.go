package main

import (
	"fmt"
	"os"
	"time"

	"github.com/spf13/cobra"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
)

var jsonOutput bool

func main() {
	root := &cobra.Command{
		Use:   "bbapi",
		Short: "Query the BuildBuddy API",
		Long: `Query the BuildBuddy Twirp JSON API (app.buildbuddy.io).

Set BUILDBUDDY_API_KEY to authenticate. Use --json for raw proto JSON output.

Potentially useful unimplemented RPCs:
  GetTargetTrends, GetStatHeatmap, GetStatDrilldown, GetDailyTargetStats,
  GetWorkflowHistory, DeleteInvocation

Full service definition:
  https://github.com/buildbuddy-io/buildbuddy/blob/master/proto/buildbuddy_service.proto`,
	}
	root.PersistentFlags().BoolVar(&jsonOutput, "json", false, "Output raw JSON")

	root.AddCommand(invocationCmd())
	root.AddCommand(targetCmd())
	root.AddCommand(executionCmd())
	root.AddCommand(cacheCmd())
	root.AddCommand(artifactCmd())
	root.AddCommand(toolLogCmd())
	root.AddCommand(trendCmd())
	root.AddCommand(workflowCmd())
	root.AddCommand(askCmd())

	if err := root.Execute(); err != nil {
		os.Exit(1)
	}
}

func printProtoJSON(msg proto.Message) error {
	b, err := protojson.MarshalOptions{Indent: "  "}.Marshal(msg)
	if err != nil {
		return err
	}
	_, err = os.Stdout.Write(b)
	fmt.Println()
	return err
}

func fmtDurationUsec(us int64) string {
	d := time.Duration(us) * time.Microsecond
	switch {
	case d >= time.Hour:
		return fmt.Sprintf("%.0fh", d.Hours())
	case d >= time.Minute:
		return fmt.Sprintf("%.0fm", d.Minutes())
	default:
		return fmt.Sprintf("%.0fs", d.Seconds())
	}
}

package main

import (
	"fmt"
	"time"

	"github.com/spf13/cobra"
	"google.golang.org/protobuf/types/known/timestamppb"

	statspb "github.com/buildbuddy-io/buildbuddy/proto/stats"
)

func trendCmd() *cobra.Command {
	var repo string
	var days int
	cmd := &cobra.Command{
		Use:   "trend",
		Short: "Show build performance trends over time",
		RunE: func(_ *cobra.Command, _ []string) error {
			c, err := newClient()
			if err != nil {
				return err
			}
			if repo == "" {
				repo, err = detectRepoURL()
				if err != nil {
					return fmt.Errorf("auto-detect repo (use --repo to override): %w", err)
				}
			}
			now := time.Now()
			after := now.AddDate(0, 0, -days)
			req := &statspb.GetTrendRequest{
				Query: &statspb.TrendQuery{
					RepoUrl:       repo,
					UpdatedAfter:  timestamppb.New(after),
					UpdatedBefore: timestamppb.New(now),
				},
			}
			resp := &statspb.GetTrendResponse{}
			if err := c.call("GetTrend", req, resp); err != nil {
				return err
			}
			if jsonOutput {
				return printProtoJSON(resp)
			}
			if s := resp.GetCurrentSummary(); s != nil {
				fmt.Printf("Summary (last %d days):\n", days)
				fmt.Printf("  Builds: %d  With cache: %d  AC hits: %d  AC misses: %d\n\n",
					s.GetNumBuilds(), s.GetNumBuildsWithRemoteCache(),
					s.GetAcCacheHits(), s.GetAcCacheMisses())
			}
			t := newTable()
			t.header("DATE", "BUILDS", "USERS", "COMMITS", "P50", "P90")
			for _, ts := range resp.GetTrendStat() {
				name := ts.GetName()
				if name == "" && ts.GetBucketStartTimeMicros() > 0 {
					name = time.UnixMicro(ts.GetBucketStartTimeMicros()).Format("2006-01-02")
				}
				t.row(
					name,
					ts.GetTotalNumBuilds(),
					ts.GetUserCount(),
					ts.GetCommitCount(),
					fmtDurationUsec(int64(ts.GetBuildTimeUsecP50())),
					fmtDurationUsec(int64(ts.GetBuildTimeUsecP90())),
				)
			}
			t.flush()
			return nil
		},
	}
	cmd.Flags().StringVar(&repo, "repo", "", "Repository URL (default: auto-detect from git)")
	cmd.Flags().IntVar(&days, "days", 7, "Number of days to look back")
	return cmd
}

package main

import (
	"context"
	"fmt"
	"time"

	"github.com/spf13/cobra"

	cachepb "github.com/buildbuddy-io/buildbuddy/proto/cache"
	repb "github.com/buildbuddy-io/buildbuddy/proto/remote_execution"
	resourcepb "github.com/buildbuddy-io/buildbuddy/proto/resource"
)

func cacheCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "cache <invocation-id>",
		Short: "Show cache scorecard (default), or use subcommands",
		Args:  cobra.ExactArgs(1),
		RunE: func(_ *cobra.Command, args []string) error {
			c, err := newClient()
			if err != nil {
				return err
			}
			defer c.close()
			req := &cachepb.GetCacheScoreCardRequest{
				InvocationId: args[0],
			}
			resp, err := c.rpc.GetCacheScoreCard(context.Background(), req)
			if err != nil {
				return err
			}
			if jsonOutput {
				return printProtoJSON(resp)
			}
			t := newTable()
			t.header("MNEMONIC", "TARGET", "CACHE")
			for _, r := range resp.GetResults() {
				t.row(r.GetActionMnemonic(), r.GetTargetId(), r.GetCacheType().String())
			}
			t.flush()
			return nil
		},
	}
	cmd.AddCommand(cacheMetadataSubCmd())
	return cmd
}

func cacheMetadataSubCmd() *cobra.Command {
	var instance string
	var cacheType string
	cmd := &cobra.Command{
		Use:   "metadata <hash> <size-bytes>",
		Short: "Get metadata for a cached artifact by digest",
		Args:  cobra.ExactArgs(2),
		RunE: func(_ *cobra.Command, args []string) error {
			c, err := newClient()
			if err != nil {
				return err
			}
			defer c.close()
			var sizeBytes int64
			if _, err := fmt.Sscanf(args[1], "%d", &sizeBytes); err != nil {
				return fmt.Errorf("invalid size %q: %w", args[1], err)
			}
			ct := resourcepb.CacheType_CAS
			if cacheType == "AC" || cacheType == "ac" {
				ct = resourcepb.CacheType_AC
			}
			req := &cachepb.GetCacheMetadataRequest{
				ResourceName: &resourcepb.ResourceName{
					Digest: &repb.Digest{
						Hash:      args[0],
						SizeBytes: sizeBytes,
					},
					InstanceName: instance,
					CacheType:    ct,
				},
			}
			resp, err := c.rpc.GetCacheMetadata(context.Background(), req)
			if err != nil {
				return err
			}
			if jsonOutput {
				return printProtoJSON(resp)
			}
			fmt.Printf("Digest size:   %d bytes\n", resp.GetDigestSizeBytes())
			fmt.Printf("Stored size:   %d bytes\n", resp.GetStoredSizeBytes())
			if ts := resp.GetLastAccessUsec(); ts > 0 {
				fmt.Printf("Last access:   %s\n", time.UnixMicro(ts).Format(time.RFC3339))
			}
			if ts := resp.GetLastModifyUsec(); ts > 0 {
				fmt.Printf("Last modified: %s\n", time.UnixMicro(ts).Format(time.RFC3339))
			}
			return nil
		},
	}
	cmd.Flags().StringVar(&instance, "instance", "", "Remote instance name")
	cmd.Flags().StringVar(&cacheType, "type", "CAS", "Cache type: CAS or AC")
	return cmd
}

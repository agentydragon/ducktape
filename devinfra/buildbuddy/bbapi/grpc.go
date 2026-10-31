package main

import (
	"context"
	"crypto/tls"
	"fmt"
	"math"
	"net"
	"net/url"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

const eventLogIndexLimitExceededMessage = "Log index limit exceeded"

func isEventLogIndexLimitExceeded(err error) bool {
	return status.Code(err) == codes.ResourceExhausted && status.Convert(err).Message() == eventLogIndexLimitExceededMessage
}

func newRPCConnection(base, key string, timeout time.Duration, retries int) (*grpc.ClientConn, error) {
	endpoint, err := url.Parse(base)
	if err != nil {
		return nil, err
	}
	var transport credentials.TransportCredentials
	switch endpoint.Scheme {
	case "https":
		transport = credentials.NewTLS(&tls.Config{MinVersion: tls.VersionTLS12})
	case "http":
		transport = insecure.NewCredentials()
	default:
		return nil, fmt.Errorf("unsupported BuildBuddy URL scheme %q", endpoint.Scheme)
	}
	if endpoint.Hostname() == "" {
		return nil, fmt.Errorf("BuildBuddy URL needs a host")
	}
	address := endpoint.Host
	if endpoint.Port() == "" {
		port := "443"
		if endpoint.Scheme == "http" {
			port = "80"
		}
		address = net.JoinHostPort(endpoint.Hostname(), port)
	}
	return grpc.NewClient(
		"dns:///"+address,
		grpc.WithTransportCredentials(transport),
		// Invocation responses can exceed grpc-go's 4 MiB default. Match the
		// previous HTTP transport, which did not impose a smaller message cap.
		grpc.WithDefaultCallOptions(grpc.MaxCallRecvMsgSize(math.MaxInt32)),
		grpc.WithUnaryInterceptor(readRPCInterceptor(key, timeout, retries)),
	)
}

func readRPCInterceptor(key string, timeout time.Duration, retries int) grpc.UnaryClientInterceptor {
	return func(ctx context.Context, method string, req, reply any, conn *grpc.ClientConn, invoke grpc.UnaryInvoker, opts ...grpc.CallOption) error {
		if timeout > 0 {
			var cancel context.CancelFunc
			ctx, cancel = context.WithTimeout(ctx, timeout)
			defer cancel()
		}
		ctx = metadata.AppendToOutgoingContext(ctx, "x-buildbuddy-api-key", key)
		// Only the read methods migrated by this CLI are retried. Any future
		// mutation using the generated client is sent once unless reviewed here.
		attempts := 1
		switch method {
		case "/buildbuddy.service.BuildBuddyService/GetInvocation",
			"/buildbuddy.service.BuildBuddyService/SearchInvocation",
			"/buildbuddy.service.BuildBuddyService/GetTarget",
			"/buildbuddy.service.BuildBuddyService/GetTargetHistory",
			"/buildbuddy.service.BuildBuddyService/GetTargetStats",
			"/buildbuddy.service.BuildBuddyService/GetTargetFlakeSamples",
			"/buildbuddy.service.BuildBuddyService/GetInvocationStat",
			"/buildbuddy.service.BuildBuddyService/GetTrend",
			"/buildbuddy.service.BuildBuddyService/GetWorkflows",
			"/buildbuddy.service.BuildBuddyService/GetEventLogChunk",
			"/buildbuddy.service.BuildBuddyService/GetExecution",
			"/buildbuddy.service.BuildBuddyService/SearchExecution",
			"/buildbuddy.service.BuildBuddyService/GetCacheScoreCard",
			"/buildbuddy.service.BuildBuddyService/GetExecutionDownloads",
			"/buildbuddy.service.BuildBuddyService/GetCacheMetadata":
			attempts += retries
		}
		var err error
		for attempt := 0; attempt < attempts; attempt++ {
			err = invoke(ctx, method, req, reply, conn, opts...)
			if method == "/buildbuddy.service.BuildBuddyService/GetEventLogChunk" && isEventLogIndexLimitExceeded(err) {
				// This is a deterministic cursor/index limit, not a transient
				// resource shortage. Let the watcher recover from the first chunk.
				return err
			}
			if status.Code(err) != codes.Unavailable && status.Code(err) != codes.ResourceExhausted {
				return err
			}
			if attempt+1 < attempts {
				timer := time.NewTimer(time.Second * time.Duration(1<<attempt))
				select {
				case <-ctx.Done():
					timer.Stop()
					return status.FromContextError(ctx.Err()).Err()
				case <-timer.C:
				}
			}
		}
		return err
	}
}

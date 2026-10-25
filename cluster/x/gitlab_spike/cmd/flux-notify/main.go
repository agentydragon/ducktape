package main

import (
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"time"
)

func run() int {
	endpoint := os.Getenv("FLUX_WEBHOOK_URL")
	if endpoint == "" {
		fmt.Fprintln(os.Stderr, "FLUX_WEBHOOK_URL is not set")
		return 1
	}
	parsed, err := url.ParseRequestURI(endpoint)
	if err != nil || (parsed.Scheme != "https" && parsed.Scheme != "http") || parsed.Host == "" {
		fmt.Fprintln(os.Stderr, "FLUX_WEBHOOK_URL is not a valid HTTP endpoint")
		return 1
	}
	request, err := http.NewRequest(http.MethodPost, endpoint, nil)
	if err != nil {
		fmt.Fprintln(os.Stderr, "could not create Flux notification request")
		return 1
	}
	client := &http.Client{
		Timeout: 10 * time.Second,
		CheckRedirect: func(_ *http.Request, _ []*http.Request) error {
			return http.ErrUseLastResponse
		},
	}
	response, err := client.Do(request)
	if err != nil {
		fmt.Fprintln(os.Stderr, "Flux Receiver request failed")
		return 1
	}
	defer response.Body.Close()
	_, _ = io.Copy(io.Discard, io.LimitReader(response.Body, 4096))
	if response.StatusCode < http.StatusOK || response.StatusCode >= http.StatusMultipleChoices {
		fmt.Fprintf(os.Stderr, "Flux Receiver returned HTTP %d\n", response.StatusCode)
		return 1
	}
	fmt.Printf("Flux Receiver accepted push notification: HTTP %d\n", response.StatusCode)
	return 0
}

func main() {
	os.Exit(run())
}

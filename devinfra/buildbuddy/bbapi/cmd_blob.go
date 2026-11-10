package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sync"

	"github.com/spf13/cobra"
)

type blobDownload struct {
	URI          string `json:"uri"`
	Destination  string `json:"destination"`
	InvocationID string `json:"invocation_id,omitempty"`
}

func writeBlob(c *client, item blobDownload) error {
	parent := filepath.Dir(item.Destination)
	if err := os.MkdirAll(parent, 0755); err != nil {
		return err
	}
	temp, err := os.CreateTemp(parent, ".bbapi-blob-*")
	if err != nil {
		return err
	}
	defer os.Remove(temp.Name())
	err = c.consumeURL(c.bytestreamDownloadURL(item.InvocationID, item.URI), func(r io.Reader) error {
		// Each retry replaces any bytes left by an interrupted response.
		if err := temp.Truncate(0); err != nil {
			return err
		}
		if _, err := temp.Seek(0, io.SeekStart); err != nil {
			return err
		}
		_, err := io.Copy(temp, r)
		return err
	})
	if err != nil {
		temp.Close()
		return err
	}
	if err := temp.Close(); err != nil {
		return err
	}
	return os.Rename(temp.Name(), item.Destination)
}

func downloadBlobs(c *client, items []blobDownload) error {
	destinations := map[string]bool{}
	for _, item := range items {
		if item.URI == "" || item.Destination == "" {
			return fmt.Errorf("each download needs a URI and destination")
		}
		path, err := filepath.Abs(item.Destination)
		if err != nil {
			return err
		}
		if destinations[path] {
			return fmt.Errorf("duplicate destination %q", item.Destination)
		}
		destinations[path] = true
	}
	jobs := make(chan int)
	errs := make([]error, len(items))
	var wg sync.WaitGroup
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for n := range jobs {
				if err := writeBlob(c, items[n]); err != nil {
					errs[n] = fmt.Errorf("download %s: %w", items[n].Destination, err)
				}
			}
		}()
	}
	for i := range items {
		jobs <- i
	}
	close(jobs)
	wg.Wait()
	return errors.Join(errs...)
}

func blobCmd() *cobra.Command {
	cmd := &cobra.Command{Use: "blob", Short: "Read blobs by their advertised URI without re-reading invocation events"}
	var invocationID string
	cat := &cobra.Command{Use: "cat <uri>", Short: "Write a blob to stdout", Args: cobra.ExactArgs(1), RunE: func(_ *cobra.Command, args []string) error {
		c, err := newClient()
		if err != nil {
			return err
		}
		defer c.close()
		body, err := c.fetchURL(c.bytestreamDownloadURL(invocationID, args[0]))
		if err != nil {
			return err
		}
		_, err = os.Stdout.Write(body)
		return err
	}}
	cat.Flags().StringVar(&invocationID, "invocation", "", "Invocation ID for legacy artifact fallback")
	var manifest string
	download := &cobra.Command{Use: "download --manifest <file>", Short: "Download a JSON array of {uri, destination, invocation_id} entries concurrently", Args: cobra.NoArgs, RunE: func(_ *cobra.Command, _ []string) error {
		c, err := newClient()
		if err != nil {
			return err
		}
		defer c.close()
		raw, err := os.ReadFile(manifest)
		if err != nil {
			return err
		}
		var items []blobDownload
		if err := json.Unmarshal(raw, &items); err != nil {
			return err
		}
		return downloadBlobs(c, items)
	}}
	download.Flags().StringVar(&manifest, "manifest", "", "JSON download manifest")
	_ = download.MarkFlagRequired("manifest")
	cmd.AddCommand(cat, download)
	return cmd
}

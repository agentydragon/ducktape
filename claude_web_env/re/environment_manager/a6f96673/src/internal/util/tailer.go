package util

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"sync"
	"time"
)

// Reconstructed from symbol: internal/util.Version
var Version = "dev"

// Reconstructed from symbol: internal/util.ErrAlreadyStarted
var ErrAlreadyStarted = errors.New("tailer already started")

// Reconstructed from symbol: internal/util.Line
//
// Line represents a line read from a tailed file, carrying either
// the text content or an error encountered during reading.
type Line struct {
	Text string
	Err  error
}

// Reconstructed from symbol: internal/util.Tailer
//
// Tailer watches a file and sends new lines on a channel.
// It polls the file at a configurable interval, reading new content
// and splitting it into lines delimited by newline characters.
type Tailer struct {
	path         string
	pollInterval time.Duration
	linesCh      chan Line
	stopCh       chan struct{}
	wg           sync.WaitGroup
	once         sync.Once
	started      bool
	mu           sync.Mutex
}

// defaultPollInterval is the default polling interval (100ms).
const defaultPollInterval = 100 * time.Millisecond

// maxLineLength is the maximum buffer size before truncation.
const maxLineLength = 2048

// readBufSize is the size of the read buffer.
const readBufSize = 4096

// Reconstructed from symbol: internal/util.NewTailer
//
// NewTailer creates a new Tailer for the given file path. It stats the file
// to verify it exists, creates channels, and returns the Tailer.
// If pollInterval is zero, it defaults to 100ms.
func NewTailer(path string, pollInterval time.Duration) (*Tailer, error) {
	_, err := os.Stat(path)
	if err != nil {
		return nil, fmt.Errorf("failed to stat file %s: %w", path, err)
	}

	linesCh := make(chan Line, 100)
	stopCh := make(chan struct{})

	if pollInterval == 0 {
		pollInterval = defaultPollInterval
	}

	return &Tailer{
		path:         path,
		pollInterval: pollInterval,
		linesCh:      linesCh,
		stopCh:       stopCh,
	}, nil
}

// Reconstructed from symbol: internal/util.(*Tailer).Lines
//
// Lines returns the channel on which tailed lines are sent.
func (t *Tailer) Lines() <-chan Line {
	return t.linesCh
}

// Reconstructed from symbol: internal/util.(*Tailer).Start
//
// Start begins tailing the file. It opens the file, seeks to the end,
// and launches a goroutine that polls for new content.
// Returns ErrAlreadyStarted if the tailer was already started.
func (t *Tailer) Start(ctx context.Context) error {
	t.mu.Lock()

	if t.started {
		t.mu.Unlock()
		return ErrAlreadyStarted
	}

	t.started = true
	t.mu.Unlock()

	file, err := os.Open(t.path)
	if err != nil {
		t.mu.Lock()
		t.started = false
		t.mu.Unlock()
		return fmt.Errorf("failed to open file %s: %w", t.path, err)
	}

	_, err = file.Seek(0, io.SeekEnd)
	if err != nil {
		file.Close()

		t.mu.Lock()
		t.started = false
		t.mu.Unlock()
		return fmt.Errorf("failed to seek to end of file %s: %w", t.path, err)
	}

	t.wg.Add(1)
	go t.run(ctx, file)

	return nil
}

// Reconstructed from symbol: internal/util.(*Tailer).Stop
//
// Stop signals the tailer to stop and waits for the run goroutine to finish.
// It uses sync.Once to ensure the stop channel is only closed once.
// If the tailer was never started, it also closes the lines channel.
func (t *Tailer) Stop() {
	t.once.Do(func() {
		close(t.stopCh)

		t.mu.Lock()
		if !t.started {
			close(t.linesCh)
		}
		t.mu.Unlock()
	})

	t.wg.Wait()
}

// Reconstructed from symbol: internal/util.(*Tailer).run
//
// run is the main polling loop. It defers cleanup (wg.Done, close linesCh,
// file.Close, ticker.Stop), then enters a select loop that reads lines on
// each tick, or exits on stop/context cancellation.
func (t *Tailer) run(ctx context.Context, file *os.File) {
	defer t.wg.Done()
	defer close(t.linesCh)
	defer file.Close()

	ticker := time.NewTicker(t.pollInterval)
	defer ticker.Stop()

	var buf []byte
	var truncated bool

	doneCh := ctx.Done()

	for {
		select {
		case <-ticker.C:
			if !t.readLines(file, &buf, &truncated) {
				return
			}
		case <-t.stopCh:
			return
		case <-doneCh:
			return
		}
	}
}

// Reconstructed from symbol: internal/util.(*Tailer).readLines
//
// readLines reads from the file into a temporary buffer, appends to the
// accumulator, and extracts complete newline-delimited lines. Each line
// (up to 2048 bytes) is sent as a Line on the lines channel. If the buffer
// exceeds 2048 bytes without a newline, it is truncated and the next partial
// line is discarded. On non-EOF read errors, sends a Line with the error and
// returns false. Returns true to continue polling, false to stop.
func (t *Tailer) readLines(file *os.File, buf *[]byte, truncated *bool) bool {
	var tmp [readBufSize]byte

	for {
		n, err := file.Read(tmp[:])

		if n > 0 {
			*buf = append(*buf, tmp[:n]...)
		}

		if err != nil {
			if err == io.EOF {
				// Process any complete lines in the buffer
				for {
					idx := bytes.IndexByte(*buf, '\n')
					if idx == -1 {
						// No complete line; check for buffer overflow
						if len(*buf) > maxLineLength {
							*truncated = true
							*buf = nil
						}
						return true
					}

					line := (*buf)[:idx]
					*buf = (*buf)[idx+1:]

					if *truncated {
						*truncated = false
						continue
					}

					if len(line) > maxLineLength {
						continue
					}

					lineStr := string(line)
					select {
					case t.linesCh <- Line{Text: lineStr}:
					case <-t.stopCh:
						return false
					}
				}
			}

			// Non-EOF error: send on channel and stop
			select {
			case t.linesCh <- Line{Err: err}:
			case <-t.stopCh:
			}
			return false
		}

	}
}

package environment_runtime_transport

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"sync"
	"time"

	"github.com/spiffe/go-spiffe/v2/bundle/x509bundle"
	"github.com/spiffe/go-spiffe/v2/spiffeid"
	"github.com/spiffe/go-spiffe/v2/svid/x509svid"
	"github.com/spiffe/go-spiffe/v2/workloadapi"
)

// workloadAPISource keeps the current Workload API material in an immutable
// snapshot. go-spiffe's X509Source is not safe for concurrent bundle reads
// during X509Context updates, so keep the snapshot pointer behind a lock and
// leave each SDK-created bundle set untouched.
type workloadAPISource struct {
	mu        sync.RWMutex
	svid      *x509svid.SVID
	bundles   *x509bundle.Set
	err       error
	closed    bool
	ready     chan struct{}
	readyOnce sync.Once
	done      chan struct{}
	client    *workloadapi.Client
	cancel    context.CancelFunc
	closeOnce sync.Once
	closeErr  error
}

func newWorkloadAPISource(ctx context.Context, address string) (credentialSource, func() error, error) {
	client, err := workloadapi.New(ctx, workloadapi.WithAddr(address))
	if err != nil {
		return nil, nil, fmt.Errorf("connect to Workload API: %w", err)
	}
	watchCtx, cancel := context.WithCancel(ctx)
	source := &workloadAPISource{
		ready:  make(chan struct{}),
		done:   make(chan struct{}),
		client: client,
		cancel: cancel,
	}
	go func() {
		defer close(source.done)
		err := client.WatchX509Context(watchCtx, source)
		if err != nil && !errors.Is(err, context.Canceled) {
			source.setError(err)
		}
	}()

	startupTimer := time.NewTimer(workloadAPISourceStartupTimeout)
	defer startupTimer.Stop()
	select {
	case <-source.ready:
		if err := source.sourceError(); err != nil {
			_ = source.close()
			return nil, nil, fmt.Errorf("watch Workload API X.509 context: %w", err)
		}
		return source, source.close, nil
	case <-ctx.Done():
		_ = source.close()
		return nil, nil, fmt.Errorf("wait for initial Workload API X.509 context: %w", ctx.Err())
	case <-startupTimer.C:
		_ = source.close()
		return nil, nil, fmt.Errorf("wait for initial Workload API X.509 context: timed out after %s", workloadAPISourceStartupTimeout)
	case <-source.done:
		err := source.sourceError()
		_ = source.close()
		if err != nil {
			return nil, nil, fmt.Errorf("watch Workload API X.509 context: %w", err)
		}
		return nil, nil, errors.New("Workload API X.509 context watch stopped before its initial update")
	}
}

func (s *workloadAPISource) OnX509ContextUpdate(x509Context *workloadapi.X509Context) {
	if x509Context == nil || len(x509Context.SVIDs) == 0 || x509Context.SVIDs[0] == nil || x509Context.Bundles == nil {
		s.setError(errors.New("Workload API returned an incomplete X.509 context"))
		return
	}
	s.mu.Lock()
	s.svid = x509Context.DefaultSVID()
	s.bundles = x509Context.Bundles
	s.mu.Unlock()
	s.readyOnce.Do(func() { close(s.ready) })
}

func (s *workloadAPISource) OnX509ContextWatchError(err error) {
	if err != nil && !errors.Is(err, context.Canceled) {
		slog.Warn("Workload API X.509 context watch failed; retaining last verified material while retrying", "error", err)
	}
}

func (s *workloadAPISource) GetX509SVID() (*x509svid.SVID, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	if s.closed {
		return nil, errors.New("Workload API source is closed")
	}
	if s.err != nil {
		return nil, s.err
	}
	if s.svid == nil {
		return nil, errors.New("Workload API has not provided an X.509 SVID")
	}
	return s.svid, nil
}

func (s *workloadAPISource) GetX509BundleForTrustDomain(td spiffeid.TrustDomain) (*x509bundle.Bundle, error) {
	s.mu.RLock()
	if s.closed {
		s.mu.RUnlock()
		return nil, errors.New("Workload API source is closed")
	}
	if s.err != nil {
		err := s.err
		s.mu.RUnlock()
		return nil, err
	}
	bundles := s.bundles
	s.mu.RUnlock()
	if bundles == nil {
		return nil, errors.New("Workload API has not provided X.509 bundles")
	}
	return bundles.GetX509BundleForTrustDomain(td)
}

func (s *workloadAPISource) setError(err error) {
	s.mu.Lock()
	if s.err == nil {
		s.err = err
	}
	s.mu.Unlock()
	s.readyOnce.Do(func() { close(s.ready) })
}

func (s *workloadAPISource) sourceError() error {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.err
}

func (s *workloadAPISource) close() error {
	s.closeOnce.Do(func() {
		s.cancel()
		s.closeErr = s.client.Close()
		<-s.done
		s.mu.Lock()
		s.closed = true
		s.mu.Unlock()
	})
	return s.closeErr
}

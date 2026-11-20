package kubeapiserver

import (
	"context"
	"testing"

	"sigs.k8s.io/controller-runtime/pkg/client"
)

type TestClient struct {
	t      testing.TB
	client client.Client
}

func NewTestClient(t testing.TB, client client.Client) TestClient {
	t.Helper()
	return TestClient{t: t, client: client}
}

func (c TestClient) Create(object client.Object) {
	c.t.Helper()
	if err := c.client.Create(context.Background(), object); err != nil {
		c.t.Fatal(err)
	}
}

func (c TestClient) Update(object client.Object) {
	c.t.Helper()
	if err := c.client.Update(context.Background(), object); err != nil {
		c.t.Fatal(err)
	}
}

func (c TestClient) Delete(object client.Object) {
	c.t.Helper()
	if err := c.client.Delete(context.Background(), object); err != nil {
		c.t.Fatal(err)
	}
}

func (c TestClient) Get(namespace, name string, object client.Object) {
	c.t.Helper()
	if err := c.client.Get(context.Background(), client.ObjectKey{Namespace: namespace, Name: name}, object); err != nil {
		c.t.Fatal(err)
	}
}

// StartForTest runs a control plane that t stops when it ends. It fails t if the control plane
// cannot start, and reports an error if it cannot stop.
func StartForTest(t testing.TB, options ...Option) *Server {
	t.Helper()
	server, err := Start(options...)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := server.Stop(); err != nil {
			t.Error(err)
		}
	})
	return server
}

func RunUntilCleanup(t testing.TB, fn func(context.Context)) {
	t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		defer close(done)
		fn(ctx)
	}()
	t.Cleanup(func() {
		cancel()
		<-done
	})
}

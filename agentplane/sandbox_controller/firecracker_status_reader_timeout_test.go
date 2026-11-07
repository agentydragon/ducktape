package sandboxcontroller

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"
)

func TestPodProxyFirecrackerStatusReaderBoundsStalledRequest(t *testing.T) {
	requestStarted := make(chan string, 1)
	requestCanceled := make(chan struct{})
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requestStarted <- r.URL.Path
		<-r.Context().Done()
		close(requestCanceled)
	}))
	defer server.Close()

	client, err := kubernetes.NewForConfig(&rest.Config{Host: server.URL})
	if err != nil {
		t.Fatalf("create Kubernetes client: %v", err)
	}
	pod := &corev1.Pod{ObjectMeta: metav1.ObjectMeta{Name: "launcher", Namespace: "test"}}

	// This caller deadline keeps a regression from wedging the test. The reader's own timeout
	// should cancel the request first and leave room before this outer bound.
	ctx, cancel := context.WithTimeout(context.Background(), 2*firecrackerStatusReadTimeout)
	defer cancel()
	start := time.Now()
	_, err = (PodProxyFirecrackerStatusReader{Client: client}).Read(ctx, pod)
	elapsed := time.Since(start)
	if !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("Read() error = %v, want context deadline exceeded", err)
	}
	if elapsed >= firecrackerStatusReadTimeout+2*time.Second {
		t.Fatalf("Read() took %s, want it bounded near %s", elapsed, firecrackerStatusReadTimeout)
	}

	select {
	case path := <-requestStarted:
		if want := "/api/v1/namespaces/test/pods/launcher:9092/proxy/state"; path != want {
			t.Fatalf("proxied request path = %q, want %q", path, want)
		}
	default:
		t.Fatal("reader returned without reaching the HTTP proxy")
	}
	select {
	case <-requestCanceled:
	case <-time.After(time.Second):
		t.Fatal("HTTP proxy did not observe cancellation after the bounded read returned")
	}
}

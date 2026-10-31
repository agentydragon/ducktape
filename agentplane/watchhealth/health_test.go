package watchhealth

import (
	"context"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/client-go/rest"
	"k8s.io/client-go/tools/cache"
)

// server answers each path with the status set for it, 200 by default.
type server struct{ statuses map[string]int }

func (s *server) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	status, ok := s.statuses[r.URL.Path]
	if !ok {
		status = http.StatusOK
	}
	w.WriteHeader(status)
}

func client(t *testing.T, health *Health, statuses map[string]int) (*http.Client, string) {
	t.Helper()
	api := httptest.NewServer(&server{statuses: statuses})
	t.Cleanup(api.Close)
	httpClient, err := rest.HTTPClientFor(health.Config(&rest.Config{Host: api.URL}, KindByPath))
	if err != nil {
		t.Fatal(err)
	}
	return httpClient, api.URL
}

func request(t *testing.T, client *http.Client, method, url string) {
	t.Helper()
	request, err := http.NewRequest(method, url, nil)
	if err != nil {
		t.Fatal(err)
	}
	response, err := client.Do(request)
	if err != nil {
		t.Fatal(err)
	}
	_ = response.Body.Close()
}

func TestUnavailableUntilSyncedAndWhileDraining(t *testing.T) {
	health := New()
	if health.Available() {
		t.Fatal("available before the first sync")
	}
	health.MarkSynced()
	if !health.Available() {
		t.Fatal("unavailable after sync with no failing kind")
	}
	health.MarkDraining()
	if status := health.Status(); status.Available || !status.Draining {
		t.Fatalf("draining status = %+v", status)
	}
}

func TestFailingListMakesKindUnhealthyUntilItsNextSuccess(t *testing.T) {
	health := New()
	health.MarkSynced()
	statuses := map[string]int{"/api/v1/namespaces/n/secrets": http.StatusInternalServerError}
	api, host := client(t, health, statuses)

	request(t, api, "GET", host+"/api/v1/namespaces/n/secrets")
	status := health.Status()
	if status.Available {
		t.Fatal("available while secrets fail")
	}
	failure, ok := status.Failing["secrets"]
	if !ok || failure.Error == "" || failure.Since.IsZero() {
		t.Fatalf("failing = %+v", status.Failing)
	}

	request(t, api, "GET", host+"/api/v1/namespaces/n/secrets")
	if again := health.Status().Failing["secrets"]; !again.Since.Equal(failure.Since) {
		t.Fatalf("a repeated failure moved its start from %v to %v", failure.Since, again.Since)
	}

	delete(statuses, "/api/v1/namespaces/n/secrets")
	request(t, api, "GET", host+"/api/v1/namespaces/n/secrets?watch=true")
	if !health.Available() {
		t.Fatalf("still unavailable after a successful watch: %+v", health.Status())
	}
}

func TestExpiredResourceVersionsAndWritesLeaveHealthAlone(t *testing.T) {
	health := New()
	health.MarkSynced()
	api, host := client(t, health, map[string]int{
		"/api/v1/namespaces/n/secrets": http.StatusGone,
		"/api/v1/namespaces/n/pods":    http.StatusInternalServerError,
	})
	request(t, api, "GET", host+"/api/v1/namespaces/n/secrets")
	request(t, api, "PATCH", host+"/api/v1/namespaces/n/pods")
	if !health.Available() {
		t.Fatalf("an expired resourceVersion or a failed write made the cache unavailable: %+v", health.Status())
	}
}

func TestWatchErrorHandlerRecordsAbnormalEnds(t *testing.T) {
	health := New()
	health.MarkSynced()
	reflector := cache.NewReflector(&cache.ListWatch{}, &corev1.Pod{}, cache.NewStore(cache.MetaNamespaceKeyFunc), 0)
	handler := health.WatchErrorHandler("pods")

	handler(context.Background(), reflector, io.EOF)
	if !health.Available() {
		t.Fatal("a normally closed watch made the cache unavailable")
	}
	handler(context.Background(), reflector, errors.New("unable to decode an event from the watch stream"))
	if _, ok := health.Status().Failing["pods"]; !ok {
		t.Fatal("an abnormal watch end left pods healthy")
	}

	api, host := client(t, health, nil)
	request(t, api, "GET", host+"/api/v1/namespaces/n/pods")
	if !health.Available() {
		t.Fatal("the next successful list left pods failing")
	}
}

package dispatcher

import (
	"slices"
	"strconv"
	"testing"

	dispatcherpb "github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/session_dispatcherpb"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

func TestDispatchWatchRejectsInvalidFilters(t *testing.T) {
	tooMany := make([]string, 17)
	for i := range tooMany {
		tooMany[i] = strconv.Itoa(i)
	}
	for _, filter := range []*dispatcherpb.DispatchFilter{
		nil,
		{},
		{Scope: &dispatcherpb.DispatchFilter_SessionId{SessionId: "test-session"}, IncludeCommand: true},
		{Scope: &dispatcherpb.DispatchFilter_SessionId{SessionId: "test-session"}, IncludeCommand: true, CommandIds: tooMany},
		{Scope: &dispatcherpb.DispatchFilter_SessionId{}},
		{Scope: &dispatcherpb.DispatchFilter_Sandbox{}},
		{Scope: &dispatcherpb.DispatchFilter_SessionId{SessionId: "test-session"}, CommandIds: make([]string, maxFilterCommands+1)},
	} {
		if _, err := dispatchFilter(filter); status.Code(err) != codes.InvalidArgument {
			t.Fatalf("filter %v returned %v", filter, err)
		}
	}
}

func TestDispatchWatchCanonicalizesCommandSetWithoutChangingRequest(t *testing.T) {
	filter := &dispatcherpb.DispatchFilter{Scope: &dispatcherpb.DispatchFilter_SessionId{SessionId: "test-session"}, CommandIds: []string{"test-b", "test-a", "test-a"}}
	canonical, err := dispatchFilter(filter)
	if err != nil {
		t.Fatal(err)
	}
	if !slices.Equal(canonical.GetCommandIds(), []string{"test-a", "test-b"}) || !slices.Equal(filter.GetCommandIds(), []string{"test-b", "test-a", "test-a"}) {
		t.Fatalf("canonical filter %v modified request %v", canonical, filter)
	}
}

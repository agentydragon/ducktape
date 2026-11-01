package api

import (
	"context"
	"io"
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/structpb"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/wait"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

const (
	allowed = action_servicepb.ActionState_ACTION_STATE_ALLOWED
	// The provider and reason a policy Decision is stored under, as the Python service stores them.
	policyProvider   = "action_policy_set"
	autoApproveCode  = "policy_set_auto_approve"
	testPolicyBinder = "test-binding"
)

// envelope is a submission of group/name with arguments under key.
func envelope(t *testing.T, key, group, name string, arguments map[string]any) *action_servicepb.SubmitActionRequestRequest {
	t.Helper()
	return &action_servicepb.SubmitActionRequestRequest{
		IdempotencyKey: key, Action: &action_servicepb.ActionIdentity{Group: group, Name: name},
		Arguments: structOf(t, arguments), Title: "test title for " + key,
	}
}

func (s *served) submitAs(t *testing.T, token string, request *action_servicepb.SubmitActionRequestRequest) (*action_servicepb.ActionRequest, error) {
	t.Helper()
	response, err := s.client.SubmitActionRequest(as(t, token), request)
	return response.GetActionRequest(), err
}

func actionPolicy(policy policyapi.ActionPolicy) policyapi.Rule {
	return policyapi.Rule{Type: "action_auto_approve", Policy: &policy}
}

func exactActions(group string, names ...string) policyapi.Rule {
	return actionPolicy(policyapi.ActionPolicy{Type: "exact_actions", Actions: map[string]policyapi.ActionNames{group: names}})
}

// bind grants the account the rules through one PolicySet and one PolicyBinding in the served API's policy namespace,
// and returns once the policy index resolves the binding.
func (s *served) bind(t *testing.T, account auth.Account, rules ...policyapi.Rule) {
	t.Helper()
	if err := policyObjects.Create(t.Context(), &policyapi.PolicySet{
		ObjectMeta: metav1.ObjectMeta{Namespace: s.policy, Name: "test-set"}, Spec: policyapi.PolicySetSpec{Rules: rules},
	}); err != nil {
		t.Fatal(err)
	}
	subject := policyapi.ServiceAccountRef{Namespace: account.Namespace, Name: account.Name}
	if err := policyObjects.Create(t.Context(), &policyapi.PolicyBinding{
		ObjectMeta: metav1.ObjectMeta{Namespace: s.policy, Name: testPolicyBinder},
		Spec:       policyapi.PolicyBindingSpec{Subject: subject, PolicySets: []string{"test-set"}},
	}); err != nil {
		t.Fatal(err)
	}
	if err := wait.PollUntilContextTimeout(t.Context(), 20*time.Millisecond, callDeadline, true, func(context.Context) (bool, error) {
		bindings := s.index.ResolveBindings(subject)
		return len(bindings) == 1 && len(bindings[0].PolicySets) == 1, nil
	}); err != nil {
		t.Fatalf("the policy index never resolved the binding: %v", err)
	}
}

// listByKey is the caller's requests submitted under key.
func (s *served) listByKey(t *testing.T, token, key string) []*action_servicepb.ActionRequest {
	t.Helper()
	listed, err := s.client.ListActionRequests(as(t, token), &action_servicepb.ListActionRequestsRequest{IdempotencyKey: &key})
	if err != nil {
		t.Fatal(err)
	}
	return listed.GetActionRequests()
}

func TestASubmissionNoPolicyDecidesWaitsForAnOperator(t *testing.T) {
	s := serve(t)
	request := envelope(t, "test-pending", "test-group", "test-action", map[string]any{"target": "test-target"})
	request.Description = new("test description")
	got, err := s.submitAs(t, callerToken, request)
	if err != nil {
		t.Fatal(err)
	}
	if got.GetState() != pending || got.GetDecision() != nil || got.GetExecution() != nil || got.GetVersion() != 1 {
		t.Errorf("receipt in %v, version %d, decision %v, execution %v; want a pending request at version 1",
			got.GetState(), got.GetVersion(), got.GetDecision(), got.GetExecution())
	}
	if got.GetIdempotencyKey() != request.GetIdempotencyKey() || !proto.Equal(got.GetAction(), request.GetAction()) ||
		!proto.Equal(got.GetArguments(), request.GetArguments()) || got.GetTitle() != request.GetTitle() ||
		got.GetDescription() != request.GetDescription() ||
		got.GetCaller().GetNamespace() != callerAccount.Namespace || got.GetCaller().GetName() != callerAccount.Name {
		t.Errorf("receipt %v does not carry the submission %v of %v", got, request, callerAccount)
	}
	read, err := s.client.GetActionRequest(as(t, callerToken), &action_servicepb.GetActionRequestRequest{RequestId: got.GetId()})
	if err != nil || !proto.Equal(read.GetActionRequest(), got) {
		t.Errorf("the stored request reads %v, %v; want the receipt", read, err)
	}
}

func TestASubmissionAPolicyAllowsArrivesAllowedWithTheObjectsItWasDecidedOn(t *testing.T) {
	s := serve(t)
	s.bind(t, callerAccount, exactActions("test-group", "test-action"))

	got, err := s.submitAs(t, callerToken, envelope(t, "test-allowed", "test-group", "test-action", map[string]any{}))
	if err != nil {
		t.Fatal(err)
	}
	decision := got.GetDecision()
	if got.GetState() != allowed || decision.GetVerdict() != action_servicepb.Verdict_VERDICT_ALLOW ||
		decision.GetProvider() != policyProvider || decision.GetReasonCode() != autoApproveCode || decision.GetOperator() != nil ||
		got.GetExecution().GetState() != action_servicepb.ExecutionState_EXECUTION_STATE_PENDING_DISPATCH {
		t.Errorf("receipt in %v, decision %v, execution %v; want allowed by the policy, awaiting dispatch",
			got.GetState(), decision, got.GetExecution())
	}
	evidence := decision.GetPolicyEvidence()
	binding := &policyapi.PolicyBinding{}
	if err := policyObjects.Get(t.Context(), client.ObjectKey{Namespace: s.policy, Name: testPolicyBinder}, binding); err != nil {
		t.Fatal(err)
	}
	set := &policyapi.PolicySet{}
	if err := policyObjects.Get(t.Context(), client.ObjectKey{Namespace: s.policy, Name: "test-set"}, set); err != nil {
		t.Fatal(err)
	}
	want := &action_servicepb.PolicyEvidence{
		// The binding as the index held it: ActionsReady may have changed it since.
		Bindings:   []*action_servicepb.BindingEvidence{{Namespace: s.policy, Name: testPolicyBinder, ResourceVersion: evidence.GetBindings()[0].GetResourceVersion()}},
		PolicySets: []*action_servicepb.PolicySetEvidence{{Namespace: s.policy, Name: "test-set", Generation: set.Generation}},
		Matched: &action_servicepb.MatchedPolicy{
			Namespace: s.policy, PolicySet: "test-set", Source: action_servicepb.PolicySource_POLICY_SOURCE_RULES,
			Index: 0, Type: action_servicepb.PolicyKind_POLICY_KIND_EXACT_ACTIONS,
		},
	}
	if !proto.Equal(evidence, want) || evidence.GetBindings()[0].GetResourceVersion() == "" {
		t.Errorf("evidence %v, want %v", evidence, want)
	}

	// An Action the policy does not list, and a caller no binding names, wait for an operator.
	other, err := s.submitAs(t, callerToken, envelope(t, "test-other", "test-group", "test-other-action", map[string]any{}))
	if err != nil || other.GetState() != pending {
		t.Errorf("an Action no policy lists: %v, %v", other.GetState(), err)
	}
	unbound, err := s.submitAs(t, otherToken, envelope(t, "test-unbound", "test-group", "test-action", map[string]any{}))
	if err != nil || unbound.GetState() != pending {
		t.Errorf("a caller no binding names: %v, %v", unbound.GetState(), err)
	}
}

func TestAPublicRepositoryPolicyAllowsOnlyWhatGitHubConfirmsPublic(t *testing.T) {
	s := serve(t)
	s.bind(t, callerAccount, actionPolicy(policyapi.ActionPolicy{
		Type: "github_public_repository", Actions: map[string]policyapi.ActionNames{"test-github": {"get_file_contents"}},
	}))
	submit := func(key, owner, repository string) *action_servicepb.ActionRequest {
		t.Helper()
		got, err := s.submitAs(t, callerToken, envelope(t, key, "test-github", "get_file_contents", map[string]any{"owner": owner, "repo": repository}))
		if err != nil {
			t.Fatalf("%s: %v", key, err)
		}
		return got
	}

	public := submit("test-public", publicOwner, publicRepository)
	want := &action_servicepb.MatchedRepository{Owner: publicOwner, Repository: publicRepository, ConfirmedPublic: true}
	if matched := public.GetDecision().GetPolicyEvidence().GetMatched(); public.GetState() != allowed ||
		matched.GetType() != action_servicepb.PolicyKind_POLICY_KIND_GITHUB_PUBLIC_REPOSITORY || !proto.Equal(matched.GetRepository(), want) {
		t.Errorf("a public repository: %v, matched %v", public.GetState(), matched)
	}
	// GitHub answers 404 for a private repository as for a missing one; a lookup that fails confirms nothing, and
	// never denies.
	for key, owner := range map[string]string{"test-private": "test-private-owner", "test-lookup-failed": failingOwner} {
		if got := submit(key, owner, "test-repo"); got.GetState() != pending || got.GetDecision() != nil {
			t.Errorf("%s: %v, decision %v; want it pending", key, got.GetState(), got.GetDecision())
		}
	}
}

func TestADecidedOnlySubmissionNoPolicyDecidesIsRefusedAndStoresNothing(t *testing.T) {
	s := serve(t)
	s.bind(t, callerAccount, exactActions("test-group", "test-action"))
	undecided := envelope(t, "test-undecided", "test-group", "test-other-action", map[string]any{})
	undecided.DecidedOnly = true
	_, err := s.submitAs(t, callerToken, undecided)
	wantCode(t, "a decided-only submission no policy decides", err, codes.PermissionDenied)
	if listed := s.listByKey(t, callerToken, undecided.GetIdempotencyKey()); len(listed) != 0 {
		t.Errorf("stored %v", listed)
	}
	// The key stays unused.
	undecided.DecidedOnly = false
	if got, err := s.submitAs(t, callerToken, undecided); err != nil || got.GetState() != pending {
		t.Errorf("the key once refused: %v, %v", got.GetState(), err)
	}

	decided := envelope(t, "test-decided", "test-group", "test-action", map[string]any{})
	decided.DecidedOnly = true
	if got, err := s.submitAs(t, callerToken, decided); err != nil || got.GetState() != allowed {
		t.Errorf("a decided-only submission a policy decides: %v, %v", got.GetState(), err)
	}
}

func TestASubmissionIsRefusedBeforeAnythingIsStored(t *testing.T) {
	s := serve(t)
	const key = "test-refused"
	changed := func(change func(*action_servicepb.SubmitActionRequestRequest)) *action_servicepb.SubmitActionRequestRequest {
		request := envelope(t, key, "test-group", "test-action", map[string]any{"target": "test-target"})
		change(request)
		return request
	}
	action := func(group, name string) func(*action_servicepb.SubmitActionRequestRequest) {
		return func(r *action_servicepb.SubmitActionRequestRequest) {
			r.Action = &action_servicepb.ActionIdentity{Group: group, Name: name}
		}
	}
	arguments := func(fields map[string]any) func(*action_servicepb.SubmitActionRequestRequest) {
		return func(r *action_servicepb.SubmitActionRequestRequest) { r.Arguments = structOf(t, fields) }
	}
	for name, tc := range map[string]struct {
		request *action_servicepb.SubmitActionRequestRequest
		code    codes.Code
	}{
		"an unknown group":                  {changed(action("test-unknown", "test-action")), codes.InvalidArgument},
		"an unknown Action":                 {changed(action("test-group", "test-unknown")), codes.InvalidArgument},
		"an Action no Executor runs":        {changed(action("test-unbound", "test-action")), codes.InvalidArgument},
		"an Action of an unavailable group": {changed(action("test-offline", "test-action")), codes.Unavailable},
		"an argument of the wrong type":     {changed(arguments(map[string]any{"target": 5})), codes.InvalidArgument},
		"an argument the schema refuses":    {changed(arguments(map[string]any{"target": strings.Repeat("t", 21)})), codes.InvalidArgument},
		"an argument the schema lacks":      {changed(arguments(map[string]any{"extra": true})), codes.InvalidArgument},
		"no arguments":                      {changed(func(r *action_servicepb.SubmitActionRequestRequest) { r.Arguments = nil }), codes.InvalidArgument},
		"no idempotency key":                {changed(func(r *action_servicepb.SubmitActionRequestRequest) { r.IdempotencyKey = "" }), codes.InvalidArgument},
		"no title":                          {changed(func(r *action_servicepb.SubmitActionRequestRequest) { r.Title = "" }), codes.InvalidArgument},
		"a title over 60 characters": {
			changed(func(r *action_servicepb.SubmitActionRequestRequest) { r.Title = strings.Repeat("t", 61) }), codes.InvalidArgument,
		},
		"a description over 250 characters": {
			changed(func(r *action_servicepb.SubmitActionRequestRequest) { r.Description = new(strings.Repeat("d", 251)) }), codes.InvalidArgument,
		},
		"a group that is not a key":           {changed(action("Test-group", "test-action")), codes.InvalidArgument},
		"no Action":                           {changed(func(r *action_servicepb.SubmitActionRequestRequest) { r.Action = nil }), codes.InvalidArgument},
		"an input schema that cannot be used": {changed(action("test-broken", "test-action")), codes.Internal},
	} {
		_, err := s.submitAs(t, callerToken, tc.request)
		wantCode(t, name, err, tc.code)
	}
	if listed, err := s.client.ListActionRequests(as(t, callerToken), &action_servicepb.ListActionRequestsRequest{}); err != nil || len(listed.GetActionRequests()) != 0 {
		t.Errorf("stored %v, %v", listed, err)
	}
	// No refusal spent the key.
	if _, err := s.submitAs(t, callerToken, changed(func(*action_servicepb.SubmitActionRequestRequest) {})); err != nil {
		t.Errorf("the key after the refusals: %v", err)
	}
}

func TestOnlyALabeledWorkloadSubmits(t *testing.T) {
	s := serve(t)
	request := envelope(t, "test-submitter", "test-group", "test-action", map[string]any{})
	for name, tc := range map[string]struct {
		token string
		code  codes.Code
	}{
		"an operator":                            {s.operatorToken, codes.PermissionDenied},
		"a service reader":                       {readerToken, codes.Unauthenticated},
		"an account without the caller label":    {unlabeledToken, codes.Unauthenticated},
		"a labeled account outside the callers'": {outsiderToken, codes.Unauthenticated},
	} {
		_, err := s.submitAs(t, tc.token, request)
		wantCode(t, name, err, tc.code)
	}
	if _, err := s.client.SubmitActionRequest(t.Context(), request); err == nil {
		t.Error("a submission without a bearer was accepted")
	}
	listed, err := s.client.ListActionRequests(as(t, s.operatorToken), &action_servicepb.ListActionRequestsRequest{})
	if err != nil || len(listed.GetActionRequests()) != 0 {
		t.Errorf("stored %v, %v", listed, err)
	}
}

func TestAnIdempotencyKeyIsAcceptedOncePerAccountAndRecoversTheRequest(t *testing.T) {
	s := serve(t)
	first, err := s.submitAs(t, callerToken, envelope(t, "test-key", "test-group", "test-action", map[string]any{}))
	if err != nil {
		t.Fatal(err)
	}
	// Whatever else the repeat says.
	_, err = s.submitAs(t, callerToken, envelope(t, "test-key", "test-group", "test-other-action", map[string]any{"other": true}))
	wantCode(t, "the key again", err, codes.AlreadyExists)
	if listed := s.listByKey(t, callerToken, "test-key"); len(listed) != 1 || !proto.Equal(listed[0], first) {
		t.Errorf("recovered %v, want the first receipt %v", listed, first)
	}
	if got, err := s.submitAs(t, otherToken, envelope(t, "test-key", "test-group", "test-action", map[string]any{})); err != nil || got.GetId() == first.GetId() {
		t.Errorf("another account's submission under the key: %v, %v", got, err)
	}
}

func TestASubmittedRequestIsWatchedUntilItFinishesOrTheClientsDeadline(t *testing.T) {
	s := serve(t)
	s.runWorker(t)
	s.bind(t, callerAccount, exactActions("test-group", "test-action"))
	ran, err := s.submitAs(t, callerToken, envelope(t, "test-runs", "test-group", "test-action", map[string]any{}))
	if err != nil {
		t.Fatal(err)
	}
	watch, err := s.client.WatchActionRequest(as(t, callerToken), &action_servicepb.WatchActionRequestRequest{RequestId: ran.GetId()})
	if err != nil {
		t.Fatal(err)
	}
	var last *action_servicepb.ActionRequest
	for {
		frame, err := watch.Recv()
		if err == io.EOF {
			break
		}
		if err != nil {
			t.Fatal(err)
		}
		last = frame.GetActionRequest()
	}
	want := structpb.NewStructValue(structOf(t, map[string]any{"test": "result"}))
	if last.GetState() != action_servicepb.ActionState_ACTION_STATE_SUCCEEDED || !proto.Equal(last.GetExecution().GetResult(), want) {
		t.Errorf("the watch ended at %v, result %v", last.GetState(), last.GetExecution().GetResult())
	}

	waiting, err := s.submitAs(t, callerToken, envelope(t, "test-waits", "test-group", "test-other-action", map[string]any{}))
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(t.Context(), time.Second)
	defer cancel()
	pendingWatch, err := s.client.WatchActionRequest(metadata.AppendToOutgoingContext(ctx, "authorization", "Bearer "+callerToken),
		&action_servicepb.WatchActionRequestRequest{RequestId: waiting.GetId(), Until: action_servicepb.WatchUntil_WATCH_UNTIL_DECISION})
	if err != nil {
		t.Fatal(err)
	}
	if frame, err := pendingWatch.Recv(); err != nil || frame.GetActionRequest().GetState() != pending {
		t.Errorf("first frame %v, %v; want the pending receipt", frame, err)
	}
	wantEnd(t, "a watch past the client's deadline", pendingWatch, codes.DeadlineExceeded)
}

func TestTheHTTPFacadeSubmits(t *testing.T) {
	s := serve(t)
	post := func(token, body string) (int, []byte) {
		t.Helper()
		call, err := http.NewRequestWithContext(t.Context(), http.MethodPost, s.url+"/v1/action-requests", strings.NewReader(body))
		if err != nil {
			t.Fatal(err)
		}
		call.Header.Set("Authorization", "Bearer "+token)
		response, err := http.DefaultClient.Do(call)
		if err != nil {
			t.Fatal(err)
		}
		defer response.Body.Close()
		answer, err := io.ReadAll(response.Body)
		if err != nil {
			t.Fatal(err)
		}
		return response.StatusCode, answer
	}
	// body submits test-action under key with the target, a JSON value.
	body := func(key, target string) string {
		return `{"idempotency_key": "` + key + `", "action": {"group": "test-group", "name": "test-action"},
			"arguments": {"target": ` + target + `}, "title": "test title"}`
	}

	code, answer := post(callerToken, body("test-http", `"test-target"`))
	submitted := &action_servicepb.SubmitActionRequestResponse{}
	if code != http.StatusOK || protojson.Unmarshal(answer, submitted) != nil || submitted.GetActionRequest().GetState() != pending {
		t.Fatalf("submission: %d %s", code, answer)
	}
	read, err := s.client.GetActionRequest(as(t, callerToken), &action_servicepb.GetActionRequestRequest{RequestId: submitted.GetActionRequest().GetId()})
	if err != nil || !proto.Equal(read.GetActionRequest(), submitted.GetActionRequest()) {
		t.Errorf("the stored request %v, %v differs from the HTTP receipt", read, err)
	}
	for name, tc := range map[string]struct {
		token, body string
		code        int
	}{
		"the key again":                   {callerToken, body("test-http", `"test-target"`), http.StatusConflict},
		"arguments the schema refuses":    {callerToken, body("test-http-invalid", `5`), http.StatusBadRequest},
		"an operator":                     {s.operatorToken, body("test-http-operator", `"test-target"`), http.StatusForbidden},
		"an account without the label":    {unlabeledToken, body("test-http-unlabeled", `"test-target"`), http.StatusUnauthorized},
		"a body that is not a submission": {callerToken, `[]`, http.StatusBadRequest},
	} {
		if code, answer := post(tc.token, tc.body); code != tc.code {
			t.Errorf("%s: %d %s, want %d", name, code, answer, tc.code)
		}
	}
}

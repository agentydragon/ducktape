package api

import (
	"context"
	"fmt"
	"net/http"
	"slices"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/informer"
	"github.com/agentydragon/ducktape/agentplane/action_service/policies"
	"github.com/agentydragon/ducktape/agentplane/action_service/store"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/policy/reader"
	"github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
	"google.golang.org/grpc/codes"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/util/wait"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

var (
	policyNamespaces atomic.Int32
	// unboundAccount is an account no binding names, in a namespace no caller is in, with no ServiceAccount.
	unboundAccount = auth.Account{Namespace: "test-elsewhere", Name: "test-unbound"}
)

// policyNamespace is a namespace of one test's own, for its PolicySets and PolicyBindings.
type policyNamespace string

func newPolicyNamespace(t *testing.T) policyNamespace {
	t.Helper()
	name := fmt.Sprintf("test-policy-%d", policyNamespaces.Add(1))
	_, err := kube.Clientset.CoreV1().Namespaces().Create(t.Context(), &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: name}}, metav1.CreateOptions{})
	if err != nil {
		t.Fatal(err)
	}
	return policyNamespace(name)
}

func create(t *testing.T, object client.Object) {
	t.Helper()
	if err := policyObjects.Create(t.Context(), object); err != nil {
		t.Fatal(err)
	}
}

func (n policyNamespace) policySet(t *testing.T, name string, rules ...policyapi.Rule) *policyapi.PolicySet {
	t.Helper()
	set := &policyapi.PolicySet{
		ObjectMeta: metav1.ObjectMeta{Namespace: string(n), Name: name}, Spec: policyapi.PolicySetSpec{Rules: rules},
	}
	create(t, set)
	return set
}

func (n policyNamespace) binding(t *testing.T, name string, subject auth.Account, sets ...string) *policyapi.PolicyBinding {
	t.Helper()
	binding := &policyapi.PolicyBinding{
		ObjectMeta: metav1.ObjectMeta{Namespace: string(n), Name: name},
		Spec: policyapi.PolicyBindingSpec{
			Subject: policyapi.ServiceAccountRef{Namespace: subject.Namespace, Name: subject.Name}, PolicySets: sets,
		},
	}
	create(t, binding)
	return binding
}

// watch runs a policy informer of the namespace and returns its index once the index is available, with what stops
// the informer. The informer runs until stopped or the test ends.
func (n policyNamespace) watch(t *testing.T) (*informer.Index, func()) {
	t.Helper()
	index := informer.NewIndex(reader.StaleAfterCycles*reader.MinWatchPeriod, []string{testNamespace})
	policyInformer, err := informer.New(kube.Config, index, string(n), reader.MinWatchPeriod)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		defer close(done)
		policyInformer.Run(ctx)
	}()
	var stopped sync.Once
	stop := func() {
		stopped.Do(func() {
			cancel()
			<-done
		})
	}
	t.Cleanup(stop)
	eventually(t, "the policy index to be available", func() bool { return index.Freshness.Available(time.Now()) })
	return index, stop
}

// eventually waits for the condition, and fails the test once callDeadline passes without it.
func eventually(t *testing.T, what string, condition func() bool) {
	t.Helper()
	err := wait.PollUntilContextTimeout(t.Context(), 20*time.Millisecond, callDeadline, true,
		func(context.Context) (bool, error) { return condition(), nil })
	if err != nil {
		t.Fatalf("gave up waiting for %s: %v", what, err)
	}
}

const (
	testGroup  = "test-group"
	testAction = "test-action"
)

// exactActions is a rule that auto-approves the test Action by its name.
func exactActions() policyapi.Rule {
	return policyapi.Rule{Type: policies.RuleType, Policy: &policyapi.ActionPolicy{
		Type: string(store.PolicyExactActions), Actions: map[string]policyapi.ActionNames{testGroup: {testAction}},
	}}
}

// refusedSchema is an argument_schema rule the CRD admits and this service refuses: its schema is no JSON Schema.
func refusedSchema() policyapi.Rule {
	return policyapi.Rule{Type: policies.RuleType, Policy: &policyapi.ActionPolicy{
		Type:    string(store.PolicyArgumentSchema),
		Actions: map[string]policyapi.ActionNames{testGroup: {testAction}},
		Schema:  &runtime.RawExtension{Raw: []byte(`{"type": 5}`)},
	}}
}

// accountView is the account as a request or a view names it.
func accountView(account auth.Account) *sandboxes.ServiceAccount {
	return serviceAccount(store.ServiceAccount(account))
}

// readPolicy is the policy the bearer is shown of the subject, its own when subject is nil.
func (s *served) readPolicy(t *testing.T, token string, subject *sandboxes.ServiceAccount) (*action_servicepb.CallerActionPolicy, error) {
	t.Helper()
	got, err := s.client.GetActionPolicy(as(t, token), &action_servicepb.GetActionPolicyRequest{Subject: subject})
	return got.GetActionPolicy(), err
}

// bindingNames are the view's bindings by name.
func bindingNames(view *action_servicepb.CallerActionPolicy) []string {
	var names []string
	for _, binding := range view.GetBindings() {
		names = append(names, binding.GetName())
	}
	return names
}

func TestEachPrincipalReadsThePolicyOfTheAccountsItMayName(t *testing.T) {
	namespace := newPolicyNamespace(t)
	namespace.policySet(t, "test-set", exactActions())
	for binding, account := range map[string]auth.Account{
		"test-caller-binding": callerAccount, "test-other-binding": otherAccount,
		"test-labeled-reader-binding": labeledReaderAccount, "test-reader-binding": readerAccount,
		"test-unlabeled-binding": unlabeledAccount,
	} {
		namespace.binding(t, binding, account, "test-set")
	}
	index, _ := namespace.watch(t)
	s := serveWith(t, kube.TokenReviews(), index)
	for name, tc := range map[string]struct {
		token string
		// subject is the account named, or nil for the caller's own.
		subject *auth.Account
		code    codes.Code
		// whose policy is shown, by account and its bindings, and whether with the operator's diagnostics.
		whose    auth.Account
		bindings []string
		operator bool
	}{
		"a caller's own":                 {token: callerToken, whose: callerAccount, bindings: []string{"test-caller-binding"}},
		"its own, named by a caller":     {token: callerToken, subject: &callerAccount, whose: callerAccount, bindings: []string{"test-caller-binding"}},
		"another account's, by a caller": {token: callerToken, subject: &otherAccount, whose: otherAccount, bindings: []string{"test-other-binding"}},
		"an unlabeled account's":         {token: callerToken, subject: &unlabeledAccount, whose: unlabeledAccount, bindings: []string{"test-unlabeled-binding"}},
		"an account no binding names":    {token: callerToken, subject: &unboundAccount, whose: unboundAccount},
		"a labeled reader's own":         {token: labeledReaderToken, whose: labeledReaderAccount, bindings: []string{"test-labeled-reader-binding"}},
		"another account's, by a labeled reader": {
			token: labeledReaderToken, subject: &callerAccount, whose: callerAccount, bindings: []string{"test-caller-binding"},
		},
		"an account's, by an operator": {
			token: s.operatorToken, subject: &callerAccount, whose: callerAccount, bindings: []string{"test-caller-binding"}, operator: true,
		},
		"an unlabeled reader's own":                        {token: readerToken, code: codes.Unauthenticated},
		"another account's, by an unlabeled reader":        {token: readerToken, subject: &callerAccount, code: codes.Unauthenticated},
		"an unlabeled workload's own":                      {token: unlabeledToken, code: codes.Unauthenticated},
		"another account's, by an unlabeled workload":      {token: unlabeledToken, subject: &callerAccount, code: codes.Unauthenticated},
		"an operator's own, which it has none of":          {token: s.operatorToken, code: codes.InvalidArgument},
		"an account outside the caller namespaces, as own": {token: outsiderToken, code: codes.Unauthenticated},
	} {
		var subject *sandboxes.ServiceAccount
		if tc.subject != nil {
			subject = accountView(*tc.subject)
		}
		got, err := s.readPolicy(t, tc.token, subject)
		if tc.code != codes.OK {
			wantCode(t, name, err, tc.code)
			continue
		}
		if err != nil {
			t.Errorf("%s: %v", name, err)
			continue
		}
		if !proto.Equal(got.GetSubject(), accountView(tc.whose)) || !got.GetSynced() || !slices.Equal(bindingNames(got), tc.bindings) {
			t.Errorf("%s: shown %v, want the policy of %v with bindings %v", name, got, tc.whose, tc.bindings)
		}
		for _, binding := range got.GetBindings() {
			if (binding.Generation != nil) != tc.operator {
				t.Errorf("%s: binding %v, want the operator's diagnostics only for an operator", name, binding)
			}
		}
	}
}

func TestASubjectMustNameBothItsNamespaceAndName(t *testing.T) {
	s := serve(t)
	for name, subject := range map[string]*sandboxes.ServiceAccount{
		"no name":      {Namespace: testNamespace},
		"no namespace": {Name: callerAccount.Name},
		"neither":      {},
	} {
		for _, token := range []string{callerToken, s.operatorToken} {
			_, err := s.readPolicy(t, token, subject)
			wantCode(t, name, err, codes.InvalidArgument)
		}
	}
}

func TestOnlyAnOperatorIsShownTheBindingDiagnostics(t *testing.T) {
	namespace := newPolicyNamespace(t)
	set := namespace.policySet(t, "test-set", exactActions())
	refused := namespace.policySet(t, "test-refused-set", refusedSchema())
	binding := &policyapi.PolicyBinding{
		ObjectMeta: metav1.ObjectMeta{Namespace: string(namespace), Name: "test-binding", Labels: map[string]string{"test-label": "test-value"}},
		Spec: policyapi.PolicyBindingSpec{
			Subject:    policyapi.ServiceAccountRef{Namespace: callerAccount.Namespace, Name: callerAccount.Name},
			PolicySets: []string{"test-set", "test-refused-set", "test-missing-set"},
		},
	}
	create(t, binding)
	index, _ := namespace.watch(t)
	s := serveWith(t, kube.TokenReviews(), index)
	// The informer judges the binding once it has listed every kind, so its first ActionsReady is its last.
	var ready *metav1.Condition
	eventually(t, "the binding's ActionsReady", func() bool {
		stored := &policyapi.PolicyBinding{}
		if err := policyObjects.Get(t.Context(), client.ObjectKeyFromObject(binding), stored); err != nil {
			t.Fatal(err)
		}
		ready = meta.FindStatusCondition(stored.Status.Conditions, informer.ActionsReady.Type)
		return ready != nil
	})

	workloadView := &action_servicepb.CallerActionPolicy{
		Subject: accountView(callerAccount), Synced: true,
		Bindings: []*action_servicepb.CallerBinding{{
			Name: "test-binding", PolicySets: []*action_servicepb.ActionPolicySet{{Name: "test-set", Generation: set.Generation}},
		}},
		AutoApproveIf: []*action_servicepb.EffectivePolicy{{
			Binding: "test-binding", PolicySet: "test-set", Source: action_servicepb.PolicySource_POLICY_SOURCE_RULES, Index: 0,
			Policy: &action_servicepb.Policy{Kind: &action_servicepb.Policy_ExactActions{ExactActions: &action_servicepb.ExactActionsPolicy{
				Actions: map[string]*action_servicepb.StringList{testGroup: {Values: []string{testAction}}},
			}}},
		}},
		AutoApprovable: []*action_servicepb.ActionIdentity{{Group: testGroup, Name: testAction}},
	}
	operatorView := proto.Clone(workloadView).(*action_servicepb.CallerActionPolicy)
	operatorView.Bindings[0] = &action_servicepb.CallerBinding{
		Name: "test-binding",
		PolicySets: []*action_servicepb.ActionPolicySet{
			{Name: "test-set", Generation: set.Generation},
			// A refusal's reason is its parser's own text: only that the set is refused is compared.
			{Name: "test-refused-set", Generation: refused.Generation, Refused: proto.String("")},
		},
		MissingPolicySets: []string{"test-missing-set"},
		Labels:            map[string]string{"test-label": "test-value"},
		Generation:        &binding.Generation,
		// A binding with a set refused or missing is not ready.
		Ready: &action_servicepb.ReadyCondition{
			Status: action_servicepb.ConditionStatus_CONDITION_STATUS_FALSE, Reason: ready.Reason, Message: ready.Message,
			ObservedGeneration: &ready.ObservedGeneration,
		},
	}
	subject := accountView(callerAccount)
	for name, tc := range map[string]struct {
		token   string
		subject *sandboxes.ServiceAccount
		want    *action_servicepb.CallerActionPolicy
	}{
		"the subject itself":                {callerToken, nil, workloadView},
		"another caller naming the subject": {otherToken, subject, workloadView},
		"a labeled reader naming it":        {labeledReaderToken, subject, workloadView},
		"an operator naming the subject":    {s.operatorToken, subject, operatorView},
	} {
		// The index learns of the condition from its watch, after the API server holds it.
		var got *action_servicepb.CallerActionPolicy
		err := wait.PollUntilContextTimeout(t.Context(), 20*time.Millisecond, callDeadline, true, func(context.Context) (bool, error) {
			var err error
			got, err = s.readPolicy(t, tc.token, tc.subject)
			for _, binding := range got.GetBindings() {
				for _, set := range binding.GetPolicySets() {
					if set.Refused != nil {
						set.Refused = proto.String("")
					}
				}
			}
			return proto.Equal(got, tc.want), err
		})
		if err != nil {
			t.Errorf("%s is shown\n%s\nwant\n%s\n%v", name, protojson.Format(got), protojson.Format(tc.want), err)
		}
	}
}

func TestAPolicyIndexThatGoesStaleShowsNothingAutoDecided(t *testing.T) {
	namespace := newPolicyNamespace(t)
	namespace.policySet(t, "test-set", exactActions())
	namespace.binding(t, "test-binding", callerAccount, "test-set")
	index, stop := namespace.watch(t)
	s := serveWith(t, kube.TokenReviews(), index)
	if got, err := s.readPolicy(t, callerToken, nil); err != nil || !got.GetSynced() || len(got.GetAutoApproveIf()) != 1 {
		t.Fatalf("the caller's policy while the index is available: %v, %v", got, err)
	}

	stop()
	eventually(t, "the policy index to go stale", func() bool { return !index.Freshness.Available(time.Now()) })
	// The binding and its set are still on the API server, but nothing auto-decides.
	unsynced := &action_servicepb.CallerActionPolicy{Subject: accountView(callerAccount)}
	for name, tc := range map[string]struct {
		token   string
		subject *sandboxes.ServiceAccount
	}{
		"the caller's own":             {callerToken, nil},
		"the caller's, to an operator": {s.operatorToken, accountView(callerAccount)},
	} {
		if got, err := s.readPolicy(t, tc.token, tc.subject); err != nil || !proto.Equal(got, unsynced) {
			t.Errorf("%s: %v, %v; want %v", name, got, err, unsynced)
		}
	}
}

func TestTheHTTPFacadeAnswersActionPolicyAsTheRPC(t *testing.T) {
	namespace := newPolicyNamespace(t)
	namespace.policySet(t, "test-set", exactActions())
	namespace.binding(t, "test-caller-binding", callerAccount, "test-set")
	namespace.binding(t, "test-other-binding", otherAccount, "test-set")
	index, _ := namespace.watch(t)
	s := serveWith(t, kube.TokenReviews(), index)
	named := fmt.Sprintf("?subject.namespace=%s&subject.name=%s", otherAccount.Namespace, otherAccount.Name)
	for name, tc := range map[string]struct {
		token, query string
		subject      *sandboxes.ServiceAccount
	}{
		"a caller's own":                 {callerToken, "", nil},
		"another account's, by a caller": {callerToken, named, accountView(otherAccount)},
		"an account's, by an operator":   {s.operatorToken, named, accountView(otherAccount)},
	} {
		code, body := s.get(t, "/v1/action-policy"+tc.query, tc.token)
		answered := &action_servicepb.GetActionPolicyResponse{}
		if code != http.StatusOK || protojson.Unmarshal(body, answered) != nil {
			t.Errorf("%s: %d %s", name, code, body)
			continue
		}
		rpc, err := s.client.GetActionPolicy(as(t, tc.token), &action_servicepb.GetActionPolicyRequest{Subject: tc.subject})
		if err != nil || !proto.Equal(answered, rpc) || len(rpc.GetActionPolicy().GetBindings()) != 1 {
			t.Errorf("%s: the HTTP answer %v differs from the RPC's %v, %v", name, answered, rpc, err)
		}
	}
	for name, tc := range map[string]struct {
		token, query string
		code         int
	}{
		"no bearer":                     {"", "", http.StatusUnauthorized},
		"an unlabeled reader":           {readerToken, "", http.StatusUnauthorized},
		"an operator naming no account": {s.operatorToken, "", http.StatusBadRequest},
		"a subject without a name":      {callerToken, "?subject.namespace=" + testNamespace, http.StatusBadRequest},
	} {
		if code, body := s.get(t, "/v1/action-policy"+tc.query, tc.token); code != tc.code {
			t.Errorf("%s: %d %s, want %d", name, code, body, tc.code)
		}
	}
}

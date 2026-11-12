// Package auth authenticates Kubernetes ServiceAccount tokens for Agentplane services.
// Each API surface constructs its own Resolver with its own audience and namespace policy.
package auth

import (
	"context"
	"errors"
	"fmt"
	"regexp"
	"slices"
	"strings"
	"time"

	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/listener"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	authenticationv1 "k8s.io/api/authentication/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/sets"
	"k8s.io/apiserver/pkg/authentication/authenticator"
	"k8s.io/apiserver/pkg/authentication/token/cache"
	"k8s.io/apiserver/pkg/authentication/user"
)

const (
	podNameClaim  = "authentication.kubernetes.io/pod-name"
	podUIDClaim   = "authentication.kubernetes.io/pod-uid"
	accountPrefix = "system:serviceaccount:"
)

// DefaultVerdictTTL is how long a Resolver reuses the API server's acceptance of a bearer.
const DefaultVerdictTTL = 30 * time.Second

var (
	ErrInvalidBearer = errors.New("invalid workload bearer")
	ErrUnavailable   = errors.New("workload authentication unavailable")
)

var bearerCredential = regexp.MustCompile(`(?i)\ABearer +([A-Za-z0-9._~+/=-]+)\z`)

// Account is the ServiceAccount identity used by caller and method allowlists.
type Account struct {
	Namespace string
	Name      string
}

// UnmarshalText reads the `namespace/name` form deployment settings write an Account in.
func (a *Account) UnmarshalText(text []byte) error {
	namespace, name, found := strings.Cut(string(text), "/")
	if !found || namespace == "" || name == "" || strings.Contains(name, "/") {
		return errors.New("service account must be namespace/name")
	}
	*a = Account{Namespace: namespace, Name: name}
	return nil
}

// Principal is the account and Pod binding established by verified workload credentials.
// It is the ServiceAccount variant of Subject, which after IDENTITY_CUTOVER only privileged
// platform components (the app, central sampler, Action Service executors, MCP edge, gateway proxy)
// present; they are authorized by component role and act for an Identity through delegation.
// TODO: rename to a name meaning "platform component caller" (ComponentPrincipal or PlatformCaller,
// to be decided) once every service admits workloads through AnySubject (IDENTITY_CUTOVER,
// agentplane/plans/task_dag.md).
// ServiceAccountUID is populated only when the resolver also verifies the canonical account UID.
type Principal struct {
	Account           Account
	ServiceAccountUID string
	PodName           string
	PodUID            string
}

// IsPodBound reports whether the resolver proved the singular Pod binding required by
// destinations that preserve an explicit Pod-bound backend caller path.
func (p Principal) IsPodBound() bool { return p.PodName != "" && p.PodUID != "" }

// Identity is an Identity in the policy namespace, as the incarnation the caller's workload
// credential names.
type Identity struct {
	Name string
	UID  string
}

// IdentityPrincipal is a workload whose credential names an Identity, with the Pod its accepted
// incarnation runs in once its owner has named it: provenance, never the subject.
type IdentityPrincipal struct {
	Identity Identity
	Workload Workload
	PodName  string
	PodUID   string
}

// Workload is the WorkloadCredential an SVID was issued under: provenance, never the subject. A
// destination that pins state to the workload's owner (History pins a Session to its Sandbox)
// reads Owner.
type Workload struct {
	Namespace, Name string
	Owner           WorkloadOwner
}

// WorkloadOwner is a WorkloadCredential's controller: a ControllerOwner, or NoController.
type WorkloadOwner interface{ isWorkloadOwner() }

// ControllerOwner is the record's controller owner reference, such as the Sandbox whose runner the
// credential authenticates. It names an object in the record's namespace.
type ControllerOwner struct {
	APIVersion, Kind, Name, UID string
}

// NoController is a record with no controller owner, which no owner revokes: a standing
// workload, or an Identity's sandbox box, which lives until it is disposed of.
type NoController struct{}

func (ControllerOwner) isWorkloadOwner() {}
func (NoController) isWorkloadOwner()    {}

// Subject is a verified workload: a ServiceAccount Principal, or an IdentityPrincipal, which only
// a workload SVID yields.
type Subject interface{ isSubject() }

func (Principal) isSubject()         {}
func (IdentityPrincipal) isSubject() {}

// ServiceAccountPrincipal is the canonical exact ServiceAccount identity without Pod proof.
type ServiceAccountPrincipal = identitypb.ServiceAccountIdentity

func serviceAccountPrincipal(account Account, uid string) *ServiceAccountPrincipal {
	return &ServiceAccountPrincipal{
		Account: &identitypb.ServiceAccount{Namespace: account.Namespace, Name: account.Name},
		Uid:     uid,
	}
}

func sameServiceAccountIdentity(left, right *ServiceAccountPrincipal) bool {
	if left == nil || right == nil {
		return left == right
	}
	leftAccount, rightAccount := left.GetAccount(), right.GetAccount()
	if leftAccount == nil || rightAccount == nil {
		return leftAccount == rightAccount && left.GetUid() == right.GetUid()
	}
	return leftAccount.GetNamespace() == rightAccount.GetNamespace() && leftAccount.GetName() == rightAccount.GetName() && left.GetUid() == right.GetUid()
}

// ReviewedCaller carries a reviewed ServiceAccount identity and, when TokenReview proves it, a
// separate Pod-bound Principal. The account identity is useful to account-scoped APIs; the Pod
// proof remains explicit so those APIs cannot accidentally weaken Principal's existing contract.
type ReviewedCaller struct {
	ServiceAccount *ServiceAccountPrincipal
	Pod            *Principal
}

// TokenReviews is the Kubernetes TokenReview Create operation. The production implementation
// is authenticationv1.TokenReviewInterface; a small interface lets tests inject the faults a real API
// server cannot be made to produce.
type TokenReviews interface {
	Create(context.Context, *authenticationv1.TokenReview, metav1.CreateOptions) (*authenticationv1.TokenReview, error)
}

// Resolver reviews each bearer with the API server and reuses its acceptance for the verdict TTL
// (the Kubernetes API server's own cached token authenticator): a revoked token, or one whose Pod
// is gone, still resolves for at most that long. A rejection or a failed review is never reused, so
// the next call asks again.
type Resolver struct {
	audience     string
	namespaces   map[string]struct{}
	verdicts     authenticator.Token
	svid         *SVIDResolver
	listenerAuth *listener.Config
}

// SubjectResolver resolves the subject admitted by an AnySubject rule, a ServiceAccount or an
// Identity. Resolver implements it; a service-owned resolver may provide another verified source.
type SubjectResolver interface {
	ResolveSubject(context.Context) (Subject, error)
}

// Option tunes a Resolver.
type Option func(*settings)

type settings struct{ verdictTTL time.Duration }

// WithVerdictTTL replaces DefaultVerdictTTL. Zero reviews every call.
func WithVerdictTTL(ttl time.Duration) Option { return func(s *settings) { s.verdictTTL = ttl } }

func NewResolver(reviews TokenReviews, audience string, namespaces []string, options ...Option) (*Resolver, error) {
	if reviews == nil || audience == "" || len(namespaces) == 0 {
		return nil, fmt.Errorf("token reviewer, audience and allowed namespaces are required")
	}
	allowed := make(map[string]struct{}, len(namespaces))
	for _, namespace := range namespaces {
		if namespace == "" {
			return nil, fmt.Errorf("allowed namespace must not be empty")
		}
		allowed[namespace] = struct{}{}
	}
	configured := settings{verdictTTL: DefaultVerdictTTL}
	for _, option := range options {
		option(&configured)
	}
	return &Resolver{
		audience: audience, namespaces: allowed,
		// Errors and rejections are not cached: failureTTL is zero.
		verdicts: cache.New(tokenReviewer{reviews: reviews, audience: audience}, false, configured.verdictTTL, 0),
	}, nil
}

// tokenReviewer is the uncached authenticator.Token the verdict cache wraps: one TokenReview per call.
type tokenReviewer struct {
	reviews  TokenReviews
	audience string
}

func (t tokenReviewer) AuthenticateToken(ctx context.Context, token string) (*authenticator.Response, bool, error) {
	review, err := t.reviews.Create(ctx, &authenticationv1.TokenReview{
		Spec: authenticationv1.TokenReviewSpec{Token: token, Audiences: []string{t.audience}},
	}, metav1.CreateOptions{})
	if err != nil {
		return nil, false, err
	}
	if review == nil || !review.Status.Authenticated || !slices.Contains(review.Status.Audiences, t.audience) {
		return nil, false, nil
	}
	info := review.Status.User
	extra := make(map[string][]string, len(info.Extra))
	for claim, values := range info.Extra {
		extra[claim] = values
	}
	return &authenticator.Response{
		Audiences: review.Status.Audiences,
		User:      &user.DefaultInfo{Name: info.Username, UID: info.UID, Groups: info.Groups, Extra: extra},
	}, true, nil
}

// Resolve adapts gRPC metadata to ResolveAuthorization. HTTP callers use the same
// authorization parser and TokenReview policy directly.
func (r *Resolver) Resolve(ctx context.Context) (Principal, error) {
	if principal, secured, err := r.resolveSVID(ctx); secured {
		return principal, err
	}
	md, ok := metadata.FromIncomingContext(ctx)
	if !ok {
		return Principal{}, status.Error(codes.Unauthenticated, ErrInvalidBearer.Error())
	}
	principal, err := r.ResolveAuthorization(ctx, md.Get("authorization"))
	switch {
	case errors.Is(err, ErrInvalidBearer):
		return Principal{}, status.Error(codes.Unauthenticated, ErrInvalidBearer.Error())
	case err != nil && ctx.Err() != nil:
		return Principal{}, status.FromContextError(ctx.Err()).Err()
	case errors.Is(err, ErrUnavailable):
		return Principal{}, status.Error(codes.Unavailable, ErrUnavailable.Error())
	default:
		return principal, err
	}
}

// ResolveReviewedCaller admits a reviewed ServiceAccount identity with or without a Pod binding.
// Destinations must still choose which kind of caller is authoritative for each operation.
func (r *Resolver) ResolveReviewedCaller(ctx context.Context) (ReviewedCaller, error) {
	if principal, secured, err := r.resolveSVID(ctx); secured {
		if err != nil {
			return ReviewedCaller{}, err
		}
		return reviewedSVIDCaller(principal), nil
	}
	return r.resolveReviewedBearer(ctx)
}

// ReviewedSubject is a reviewed ServiceAccount caller, or an IdentityPrincipal, which only a
// workload SVID yields.
type ReviewedSubject interface{ isReviewedSubject() }

func (ReviewedCaller) isReviewedSubject()    {}
func (IdentityPrincipal) isReviewedSubject() {}

// resolveReviewedSubject is ResolveReviewedCaller for a method that also admits Identity callers.
func (r *Resolver) resolveReviewedSubject(ctx context.Context) (ReviewedSubject, error) {
	subject, secured, err := r.resolveSVIDSubject(ctx)
	if !secured {
		return r.resolveReviewedBearer(ctx)
	}
	if err != nil {
		return nil, err
	}
	switch subject := subject.(type) {
	case Principal:
		return reviewedSVIDCaller(subject), nil
	case IdentityPrincipal:
		return subject, nil
	default:
		return nil, status.Error(codes.Unauthenticated, ErrInvalidBearer.Error())
	}
}

func reviewedSVIDCaller(principal Principal) ReviewedCaller {
	caller := ReviewedCaller{ServiceAccount: serviceAccountPrincipal(principal.Account, principal.ServiceAccountUID)}
	// A standing workload's credential proves no Pod.
	if principal.IsPodBound() {
		caller.Pod = &principal
	}
	return caller
}

func (r *Resolver) resolveReviewedBearer(ctx context.Context) (ReviewedCaller, error) {
	md, ok := metadata.FromIncomingContext(ctx)
	if !ok {
		return ReviewedCaller{}, status.Error(codes.Unauthenticated, ErrInvalidBearer.Error())
	}
	caller, err := r.ResolveReviewedCallerAuthorization(ctx, md.Get("authorization"))
	switch {
	case errors.Is(err, ErrInvalidBearer):
		return ReviewedCaller{}, status.Error(codes.Unauthenticated, ErrInvalidBearer.Error())
	case err != nil && ctx.Err() != nil:
		return ReviewedCaller{}, status.FromContextError(ctx.Err()).Err()
	case errors.Is(err, ErrUnavailable):
		return ReviewedCaller{}, status.Error(codes.Unavailable, ErrUnavailable.Error())
	default:
		return caller, err
	}
}

// ResolveAuthorization accepts exactly one Bearer value and requires the reviewed audience,
// ServiceAccount UID and singular Pod binding claims.
func (r *Resolver) ResolveAuthorization(ctx context.Context, values []string) (Principal, error) {
	token, err := BearerToken(values)
	if err != nil {
		return Principal{}, err
	}
	return r.ResolveToken(ctx, token)
}

// ResolveReviewedCallerAuthorization accepts exactly one bearer that proves a served
// ServiceAccount. A complete optional Pod binding is preserved separately; partial claims fail.
func (r *Resolver) ResolveReviewedCallerAuthorization(ctx context.Context, values []string) (ReviewedCaller, error) {
	token, err := BearerToken(values)
	if err != nil {
		return ReviewedCaller{}, err
	}
	return r.ResolveReviewedCallerToken(ctx, token)
}

// BearerToken reads the token from exactly one RFC 6750 Bearer value. A door
// that admits more than workload tokens reads the bearer before choosing who
// verifies it.
func BearerToken(values []string) (string, error) {
	if len(values) != 1 {
		return "", ErrInvalidBearer
	}
	match := bearerCredential.FindStringSubmatch(values[0])
	if match == nil {
		return "", ErrInvalidBearer
	}
	return match[1], nil
}

// ResolveToken reviews one bearer token, however the caller received it. It fails with
// Unauthenticated when the token does not prove a workload this Resolver serves and with
// Unavailable when the TokenReview could not be answered.
func (r *Resolver) ResolveToken(ctx context.Context, token string) (Principal, error) {
	account, extra, err := r.resolveServiceAccountToken(ctx, token)
	if err != nil {
		return Principal{}, err
	}
	podName, okName := oneClaim(extra[podNameClaim])
	podUID, okUID := oneClaim(extra[podUIDClaim])
	if !okName || !okUID {
		return Principal{}, ErrInvalidBearer
	}
	identityAccount := account.GetAccount()
	return Principal{
		Account: Account{Namespace: identityAccount.GetNamespace(), Name: identityAccount.GetName()}, ServiceAccountUID: account.GetUid(),
		PodName: podName, PodUID: podUID,
	}, nil
}

// ResolveReviewedCallerToken reviews one bearer for a ServiceAccount in this Resolver's namespace
// policy. It preserves a complete Pod binding when present and allows an account-only bearer.
func (r *Resolver) ResolveReviewedCallerToken(ctx context.Context, token string) (ReviewedCaller, error) {
	account, extra, err := r.resolveServiceAccountToken(ctx, token)
	if err != nil {
		return ReviewedCaller{}, err
	}
	_, hasName := extra[podNameClaim]
	_, hasUID := extra[podUIDClaim]
	if !hasName && !hasUID {
		return ReviewedCaller{ServiceAccount: account}, nil
	}
	podName, okName := oneClaim(extra[podNameClaim])
	podUID, okUID := oneClaim(extra[podUIDClaim])
	if !okName || !okUID {
		return ReviewedCaller{}, ErrInvalidBearer
	}
	identityAccount := account.GetAccount()
	pod := Principal{
		Account: Account{Namespace: identityAccount.GetNamespace(), Name: identityAccount.GetName()}, ServiceAccountUID: account.GetUid(),
		PodName: podName, PodUID: podUID,
	}
	return ReviewedCaller{ServiceAccount: account, Pod: &pod}, nil
}

// ResolveServiceAccountToken requires the reviewed audience, namespace and ServiceAccount UID, without requiring
// a Pod binding. It is for destinations whose authority is the account alone; it does not prove a Pod identity.
func (r *Resolver) ResolveServiceAccountToken(ctx context.Context, token string) (*ServiceAccountPrincipal, error) {
	account, extra, err := r.resolveServiceAccountToken(ctx, token)
	if err != nil {
		return nil, err
	}
	_, hasName := extra[podNameClaim]
	_, hasUID := extra[podUIDClaim]
	if hasName || hasUID {
		_, validName := oneClaim(extra[podNameClaim])
		_, validUID := oneClaim(extra[podUIDClaim])
		if !validName || !validUID {
			return nil, ErrInvalidBearer
		}
	}
	return account, nil
}

func (r *Resolver) resolveServiceAccountToken(ctx context.Context, token string) (*ServiceAccountPrincipal, map[string][]string, error) {
	response, authenticated, err := r.verdicts.AuthenticateToken(ctx, token)
	if err != nil {
		return nil, nil, fmt.Errorf("%w: %w", ErrUnavailable, err)
	}
	if !authenticated {
		return nil, nil, ErrInvalidBearer
	}
	parts := strings.Split(response.User.GetName(), ":")
	if len(parts) != 4 || parts[0]+":"+parts[1]+":" != accountPrefix || parts[2] == "" || parts[3] == "" || response.User.GetUID() == "" {
		return nil, nil, ErrInvalidBearer
	}
	if _, ok := r.namespaces[parts[2]]; !ok {
		return nil, nil, ErrInvalidBearer
	}
	return serviceAccountPrincipal(Account{Namespace: parts[2], Name: parts[3]}, response.User.GetUID()), response.User.GetExtra(), nil
}

// RequireAccount applies an API/method-specific allowlist after token validation. It does not
// grant wake or command permission merely because an account can call the Gateway proxy.
func (r *Resolver) RequireAccount(ctx context.Context, allowed sets.Set[Account]) (Principal, error) {
	principal, err := r.Resolve(ctx)
	if err != nil {
		return Principal{}, err
	}
	if !allowed.Has(principal.Account) {
		return Principal{}, status.Error(codes.PermissionDenied, "service caller not allowed")
	}
	return principal, nil
}

func oneClaim(values []string) (string, bool) {
	returnValue := ""
	if len(values) != 1 || values[0] == "" {
		return returnValue, false
	}
	return values[0], true
}

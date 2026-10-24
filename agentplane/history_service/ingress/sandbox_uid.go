package ingress

import (
	"context"
	"sync"

	"github.com/agentydragon/ducktape/agentplane/history_service/storage"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"
)

// SandboxUIDResolver finds the Sandbox incarnation that owns a runner's ServiceAccount.
type SandboxUIDResolver interface {
	Resolve(context.Context, storage.Writer) (string, error)
}

// ServiceAccountSandboxUIDResolver follows the Sandbox controller's ownerReference on each
// Sandbox-specific ServiceAccount. Cache entries are keyed by the immutable ServiceAccount UID.
type ServiceAccountSandboxUIDResolver struct {
	client kubernetes.Interface
	mu     sync.RWMutex
	byUID  map[string]string
}

func NewServiceAccountSandboxUIDResolver(client kubernetes.Interface) *ServiceAccountSandboxUIDResolver {
	return &ServiceAccountSandboxUIDResolver{client: client, byUID: make(map[string]string)}
}

func (r *ServiceAccountSandboxUIDResolver) Resolve(ctx context.Context, writer storage.Writer) (string, error) {
	account, err := r.client.CoreV1().ServiceAccounts(writer.Namespace).Get(ctx, writer.Name, metav1.GetOptions{})
	if err != nil {
		return "", err
	}
	if string(account.UID) != writer.UID {
		return "", status.Error(codes.PermissionDenied, "writer ServiceAccount identity changed")
	}

	r.mu.RLock()
	uid, cached := r.byUID[writer.UID]
	r.mu.RUnlock()
	if cached {
		return uid, nil
	}
	for _, owner := range account.OwnerReferences {
		if owner.Controller != nil && *owner.Controller && owner.APIVersion == sandboxapi.GroupVersion.String() &&
			owner.Kind == sandboxapi.SandboxKind {
			uid = string(owner.UID)
			break
		}
	}
	r.mu.Lock()
	r.byUID[writer.UID] = uid
	r.mu.Unlock()
	return uid, nil
}

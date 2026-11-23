package dispatcher

import (
	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
)

func serviceAccountIdentityFor(account auth.Account, uid string) *identitypb.ServiceAccountIdentity {
	return &identitypb.ServiceAccountIdentity{
		Account: &identitypb.ServiceAccount{Namespace: account.Namespace, Name: account.Name}, Uid: uid,
	}
}

func accountOfIdentity(identity *identitypb.ServiceAccountIdentity) auth.Account {
	if identity == nil || identity.GetAccount() == nil {
		return auth.Account{}
	}
	return auth.Account{Namespace: identity.GetAccount().GetNamespace(), Name: identity.GetAccount().GetName()}
}

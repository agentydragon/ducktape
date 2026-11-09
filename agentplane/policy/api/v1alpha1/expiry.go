package v1alpha1

import (
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// Live reports whether an object with this expiresAt still grants at now. The Policy Controller
// deletes an expired object; until it does, the shared reader and the controller treat it as absent,
// so a controller outage does not extend a grant.
func Live(expiresAt *metav1.Time, now time.Time) bool {
	return expiresAt == nil || now.Before(expiresAt.Time)
}

package model

// ServiceAccount is comparable so ownership checks use namespace/name values, not protobuf pointer identity.
type ServiceAccount struct {
	Namespace string `json:"namespace"`
	Name      string `json:"name"`
}

// Identity is an Identity incarnation in the policy namespace; the UID distinguishes one recreated
// under the same name.
type Identity struct {
	Name string
	UID  string
}

// DestinationRef identifies the Sandbox a Session runs in; UID distinguishes a Sandbox recreated under the same name.
type DestinationRef struct {
	Namespace string `json:"namespace"`
	Name      string `json:"name"`
	UID       string `json:"uid"`
}

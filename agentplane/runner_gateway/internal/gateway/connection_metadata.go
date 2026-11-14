package gateway

const (
	environmentRuntimeLeaseSuffix      = "-environment-runtime"
	environmentRuntimeConnectionLabel  = "agentplane.allegedly.works/environment-runtime-connection"
	connectionEpochAnnotation          = "agentplane.allegedly.works/connection-epoch"
	connectionLeaseUIDAnnotation       = "agentplane.allegedly.works/connection-lease-uid"
	podUIDAnnotation                   = "agentplane.allegedly.works/pod-uid"
	lastConnectedPodUIDAnnotation      = "agentplane.allegedly.works/last-connected-pod-uid"
	environmentRuntimeBootIDAnnotation = "agentplane.allegedly.works/environment-runtime-boot-id"
)

// lastConnectedPodUID identifies a verified runner, independently of the current observation claim.
// A boot ID also proves contact on Leases written before the durable annotation was introduced.
func lastConnectedPodUID(annotations map[string]string) string {
	if uid := annotations[lastConnectedPodUIDAnnotation]; uid != "" {
		return uid
	}
	if annotations[environmentRuntimeBootIDAnnotation] != "" {
		return annotations[podUIDAnnotation]
	}
	return ""
}

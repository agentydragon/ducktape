package v1alpha1

// PolicySetUnavailable is the reason of a condition saying a binding names a PolicySet its reader
// cannot use, so one missing set reads the same in every reader's condition.
const PolicySetUnavailable = "PolicySetUnavailable"

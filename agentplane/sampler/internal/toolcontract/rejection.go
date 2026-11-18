package toolcontract

// Rejection is a caller-visible tool failure shared by sampler-side Code Mode validation and the
// concrete Executor. The Executor may wrap it when a remote effect has an unknown outcome.
type Rejection struct {
	Message string
}

func (e *Rejection) Error() string { return e.Message }

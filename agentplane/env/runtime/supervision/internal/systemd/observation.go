package systemd

// Observation is manager evidence for one native unit attempt. Revision orders
// callbacks from concurrent backend workers; the process controller retains the
// newest one in its attempt's status until the owner destroys the attempt.
type Observation struct {
	Revision             uint64
	Unit                 string
	StartState           StartState
	StartError           string
	ExpectedInvocationID string
	CapturedControlGroup string
	Snapshot             UnitSnapshot
	Error                string
	ExitState            ExitState
	ExitCode             int
	ExitError            string
}

type ExitState uint8

const (
	ExitPending ExitState = iota
	ExitObserved
	ExitUnknown
)

func (state ExitState) String() string {
	switch state {
	case ExitPending:
		return "pending"
	case ExitObserved:
		return "observed"
	case ExitUnknown:
		return "unknown"
	default:
		return "invalid"
	}
}

// StartState records the reply to one start request. Unconfirmed means the
// request may have reached systemd but its acceptance reply was lost; callers
// must retain this handle and fence it before they may attempt a restart.
type StartState uint8

const (
	StartPreparing StartState = iota
	StartSubmitted
	StartAccepted
	StartUnconfirmed
)

func (state StartState) String() string {
	switch state {
	case StartPreparing:
		return "preparing"
	case StartSubmitted:
		return "submitted"
	case StartAccepted:
		return "accepted"
	case StartUnconfirmed:
		return "unconfirmed"
	default:
		return "invalid"
	}
}

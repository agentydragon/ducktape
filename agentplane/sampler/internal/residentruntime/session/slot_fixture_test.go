package session

// Direct advance tests do not schedule or drain; other slot calls are unexpected.
type testSlot struct{ Slot }

func (testSlot) Drain() <-chan struct{} { return nil }

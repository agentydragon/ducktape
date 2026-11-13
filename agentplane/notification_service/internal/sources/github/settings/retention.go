package settings

import "errors"

// Retention configures GitHub receipt and observation retention independently of enabling the source.
// The YAML key stays at the Notification Service's top level.
type Retention struct {
	Days *int `mapstructure:"github_delivery_retention_days"`
}

func DefaultRetention() Retention { return Retention{Days: new(7)} }

func (r Retention) Validate() error {
	if r.Days != nil && (*r.Days < 1 || *r.Days > 3650) {
		return errors.New("github_delivery_retention_days must be null or 1 to 3650")
	}
	return nil
}

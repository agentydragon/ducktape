package settings

import "errors"

// Retention configures GitHub body, fact and observation retention independently of enabling the source.
// The YAML keys stay at the Notification Service's top level.
type Retention struct {
	Days        *int `mapstructure:"github_delivery_retention_days"`
	PayloadDays *int `mapstructure:"github_delivery_payload_retention_days"`
}

func DefaultRetention() Retention { return Retention{Days: new(7), PayloadDays: new(3)} }

func (r Retention) Validate() error {
	if r.Days != nil && (*r.Days < 1 || *r.Days > 3650) {
		return errors.New("github_delivery_retention_days must be null or 1 to 3650")
	}
	if r.PayloadDays != nil && (*r.PayloadDays < 0 || *r.PayloadDays > 3650) {
		return errors.New("github_delivery_payload_retention_days must be null or 0 to 3650")
	}
	if r.PayloadDays == nil && r.Days != nil {
		return errors.New("infinite GitHub payload retention requires infinite github_delivery_retention_days")
	}
	if r.PayloadDays != nil && r.Days != nil && *r.PayloadDays > *r.Days {
		return errors.New("github_delivery_payload_retention_days cannot exceed github_delivery_retention_days")
	}
	return nil
}

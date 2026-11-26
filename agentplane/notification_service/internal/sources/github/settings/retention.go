package settings

import "errors"

// Retention configures GitHub body, fact and observation retention independently of enabling the source.
// The YAML keys live in the Notification Service's github block.
type Retention struct {
	Hours        *int `mapstructure:"fact_retention_hours"`
	PayloadHours *int `mapstructure:"raw_body_retention_hours"`
}

func DefaultRetention() Retention { return Retention{Hours: new(168), PayloadHours: new(72)} }

func (r Retention) Validate() error {
	if r.Hours != nil && (*r.Hours < 1 || *r.Hours > 87600) {
		return errors.New("fact_retention_hours must be null or 1 to 87600")
	}
	if r.PayloadHours != nil && (*r.PayloadHours < 0 || *r.PayloadHours > 87600) {
		return errors.New("raw_body_retention_hours must be null or 0 to 87600")
	}
	if r.PayloadHours == nil && r.Hours != nil {
		return errors.New("infinite GitHub payload retention requires infinite fact_retention_hours")
	}
	if r.PayloadHours != nil && r.Hours != nil && *r.PayloadHours > *r.Hours {
		return errors.New("raw_body_retention_hours cannot exceed fact_retention_hours")
	}
	return nil
}

package responses

import "errors"

type Dialect string

const (
	DialectLite     Dialect = "lite"
	DialectStandard Dialect = "standard"
)

type Config struct {
	Dialect Dialect `json:"dialect"`
}

func (config Config) Validate() error {
	if config.Dialect != DialectLite && config.Dialect != DialectStandard {
		return errors.New("responses.dialect must be lite or standard")
	}
	return nil
}

func (config Config) Compatible(other Config) bool {
	return config == other
}

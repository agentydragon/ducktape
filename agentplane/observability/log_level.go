package observability

import (
	"errors"
	"log/slog"
)

var logLevels = map[string]slog.Level{
	"DEBUG":   slog.LevelDebug,
	"INFO":    slog.LevelInfo,
	"WARNING": slog.LevelWarn,
	"ERROR":   slog.LevelError,
}

// RootLogLevel maps the `log_levels` setting (`agentplane/log_levels.py`) to the level of a Go service's
// one slog logger, INFO unless `root` names another. Go services have no named loggers, so naming one
// is rejected rather than ignored.
func RootLogLevel(levels map[string]string) (slog.Level, error) {
	for logger, name := range levels {
		if logger != "root" {
			return 0, errors.New(`log_levels may only name "root"`)
		}
		if _, ok := logLevels[name]; !ok {
			return 0, errors.New("log_levels values must be DEBUG, INFO, WARNING, or ERROR")
		}
	}
	if name, ok := levels["root"]; ok {
		return logLevels[name], nil
	}
	return slog.LevelInfo, nil
}

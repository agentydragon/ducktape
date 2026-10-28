package runner

import (
	"context"
	"errors"
	"flag"
)

func CLI(ctx context.Context, args []string) error {
	flags := flag.NewFlagSet("agentplane-runner", flag.ContinueOnError)
	configPath := flags.String("config", "", "Versioned Runner configuration JSON")
	launchPath := flags.String("launch-file", "", "Sandbox identity and bootstrap JSON")
	if err := flags.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return nil
		}
		return err
	}
	if flags.NArg() != 0 || *configPath == "" || *launchPath == "" {
		return errors.New("--config and --launch-file are required; positional arguments are forbidden")
	}
	config, ingress, err := LoadConfiguration(*configPath, *launchPath)
	if err != nil {
		return err
	}
	runner, err := New(config)
	if err != nil {
		return err
	}
	return runner.Run(ctx, ingress)
}

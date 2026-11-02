package bootstrap

import (
	"bufio"
	"bytes"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sync"

	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
)

// Log is the append-only, newline-delimited protobuf JSON log for the
// Sandbox's one bootstrap attempt.
type Log struct {
	path   string
	file   *os.File
	mu     sync.Mutex
	events []*sessionpb.InitializationEvent
}

func OpenLog(path string) (*Log, error) {
	log := &Log{path: path}
	created := false
	if _, err := os.Stat(path); err == nil {
		if err := log.load(); err != nil {
			return nil, err
		}
	} else {
		if !errors.Is(err, os.ErrNotExist) {
			return nil, err
		}
		created = true
	}
	file, err := os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o666)
	if err != nil {
		return nil, err
	}
	if created {
		directory, err := os.Open(filepath.Dir(path))
		if err == nil {
			err = directory.Sync()
			err = errors.Join(err, directory.Close())
		}
		if err != nil {
			return nil, errors.Join(err, file.Close())
		}
	}
	log.file = file
	return log, nil
}

func (l *Log) load() error {
	data, err := os.ReadFile(l.path)
	if err != nil {
		return err
	}
	reader := bufio.NewReader(bytes.NewReader(data))
	offset := 0
	for {
		raw, readErr := reader.ReadBytes('\n')
		if len(raw) == 0 && errors.Is(readErr, io.EOF) {
			return nil
		}
		line := bytes.TrimSpace(raw)
		if len(line) > 0 {
			event := &sessionpb.InitializationEvent{}
			parseErr := protojson.UnmarshalOptions{}.Unmarshal(line, event)
			if parseErr != nil {
				incompleteTail := errors.Is(readErr, io.EOF) && len(raw) > 0 && raw[len(raw)-1] != '\n' && offset+len(raw) == len(data)
				if !incompleteTail {
					return fmt.Errorf("corrupt initialization log %s at byte %d: %w", l.path, offset, parseErr)
				}
				file, err := os.OpenFile(l.path, os.O_WRONLY, 0)
				if err != nil {
					return err
				}
				truncateErr := file.Truncate(int64(offset))
				syncErr := file.Sync()
				closeErr := file.Close()
				return errors.Join(truncateErr, syncErr, closeErr)
			}
			l.events = append(l.events, event)
		}
		offset += len(raw)
		if errors.Is(readErr, io.EOF) {
			return nil
		}
		if readErr != nil {
			return readErr
		}
	}
}

func (l *Log) LastSequence() uint64 {
	l.mu.Lock()
	defer l.mu.Unlock()
	if len(l.events) == 0 {
		return 0
	}
	return l.events[len(l.events)-1].GetSequence()
}

func (l *Log) Ended() bool {
	l.mu.Lock()
	defer l.mu.Unlock()
	if len(l.events) == 0 {
		return false
	}
	observation := l.events[len(l.events)-1].ProtoReflect().WhichOneof(
		l.events[len(l.events)-1].ProtoReflect().Descriptor().Oneofs().ByName("observation"),
	)
	return observation != nil && (observation.Name() == "result" || observation.Name() == "interrupted")
}

func (l *Log) Since(afterSequence uint64) []*sessionpb.InitializationEvent {
	l.mu.Lock()
	defer l.mu.Unlock()
	if afterSequence >= uint64(len(l.events)) {
		return []*sessionpb.InitializationEvent{}
	}
	result := make([]*sessionpb.InitializationEvent, 0, len(l.events)-int(afterSequence))
	for _, event := range l.events[afterSequence:] {
		result = append(result, proto.Clone(event).(*sessionpb.InitializationEvent))
	}
	return result
}

func (l *Log) AppendOutput(attempt uint32, stream sessionpb.InitializationStream, data []byte) error {
	event := &sessionpb.InitializationEvent{
		Attempt: attempt,
		Observation: &sessionpb.InitializationEvent_Output{Output: &sessionpb.InitializationOutput{
			Stream: stream, Data: append([]byte(nil), data...),
		}},
	}
	return l.append(event)
}

func (l *Log) AppendResult(attempt uint32, exitCode int32) (*sessionpb.InitializeResult, error) {
	result := &sessionpb.InitializeResult{Executed: true, ExitCode: exitCode}
	event := &sessionpb.InitializationEvent{
		Attempt:     attempt,
		Observation: &sessionpb.InitializationEvent_Result{Result: proto.Clone(result).(*sessionpb.InitializeResult)},
	}
	if err := l.append(event); err != nil {
		return nil, err
	}
	return result, nil
}

func (l *Log) AppendInterrupted(attempt uint32) error {
	return l.append(&sessionpb.InitializationEvent{
		Attempt:     attempt,
		Observation: &sessionpb.InitializationEvent_Interrupted{Interrupted: &sessionpb.InitializationInterrupted{}},
	})
}

func (l *Log) append(event *sessionpb.InitializationEvent) error {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.file == nil {
		return errors.New("initialization log is closed")
	}
	if len(l.events) > 0 {
		event.Sequence = l.events[len(l.events)-1].GetSequence() + 1
	} else {
		event.Sequence = 1
	}
	data, err := (protojson.MarshalOptions{UseProtoNames: true}).Marshal(event)
	if err != nil {
		return err
	}
	data = append(data, '\n')
	for len(data) > 0 {
		written, err := l.file.Write(data)
		if err != nil {
			return err
		}
		if written == 0 {
			return io.ErrShortWrite
		}
		data = data[written:]
	}
	if err := l.file.Sync(); err != nil {
		return err
	}
	l.events = append(l.events, proto.Clone(event).(*sessionpb.InitializationEvent))
	return nil
}

func (l *Log) Close() error {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.file == nil {
		return nil
	}
	file := l.file
	l.file = nil
	return file.Close()
}

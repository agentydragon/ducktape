package runner

import (
	"context"
	"fmt"
	"log/slog"
	"time"

	"go.uber.org/zap"
	"go.uber.org/zap/zapcore"
)

type slogZapCore struct {
	handler slog.Handler
}

func cosiLogger() *zap.Logger {
	return zap.New(&slogZapCore{handler: slog.Default().Handler()})
}

func (core *slogZapCore) Enabled(level zapcore.Level) bool {
	return core.handler.Enabled(context.Background(), slogLevel(level))
}

func (core *slogZapCore) With(fields []zapcore.Field) zapcore.Core {
	cloned := *core
	if len(fields) > 0 {
		cloned.handler = core.handler.WithAttrs(zapFieldsToSlogAttrs(fields))
	}
	return &cloned
}

func (core *slogZapCore) Check(entry zapcore.Entry, checked *zapcore.CheckedEntry) *zapcore.CheckedEntry {
	if core.Enabled(entry.Level) {
		return checked.AddCore(entry, core)
	}
	return checked
}

func (core *slogZapCore) Write(entry zapcore.Entry, fields []zapcore.Field) error {
	if !core.Enabled(entry.Level) {
		return nil
	}

	attrs := make([]slog.Attr, 0, len(fields)+1)
	if entry.LoggerName != "" {
		attrs = append(attrs, slog.String("logger", entry.LoggerName))
	}
	attrs = append(attrs, zapFieldsToSlogAttrs(fields)...)

	record := slog.NewRecord(entry.Time, slogLevel(entry.Level), entry.Message, entry.Caller.PC)
	record.AddAttrs(attrs...)
	return core.handler.Handle(context.Background(), record)
}

func (core *slogZapCore) Sync() error {
	return nil
}

func slogLevel(level zapcore.Level) slog.Level {
	switch {
	case level < zapcore.InfoLevel:
		return slog.LevelDebug
	case level < zapcore.WarnLevel:
		return slog.LevelInfo
	case level < zapcore.ErrorLevel:
		return slog.LevelWarn
	default:
		return slog.LevelError
	}
}

func zapFieldsToSlogAttrs(fields []zapcore.Field) []slog.Attr {
	if len(fields) == 0 {
		return nil
	}

	encoder := newZapSlogObjectEncoder()
	for _, field := range fields {
		field.AddTo(encoder)
	}
	return encoder.slogAttrs()
}

type zapSlogObjectEncoder struct {
	attrs      []slog.Attr
	current    *[]slog.Attr
	namespaces []zapSlogNamespace
}

type zapSlogNamespace struct {
	parent *[]slog.Attr
	index  int
	child  *[]slog.Attr
}

func newZapSlogObjectEncoder() *zapSlogObjectEncoder {
	encoder := &zapSlogObjectEncoder{}
	encoder.current = &encoder.attrs
	return encoder
}

func (encoder *zapSlogObjectEncoder) slogAttrs() []slog.Attr {
	for index := len(encoder.namespaces) - 1; index >= 0; index-- {
		namespace := encoder.namespaces[index]
		(*namespace.parent)[namespace.index].Value = slog.GroupValue(*namespace.child...)
	}
	return encoder.attrs
}

func (encoder *zapSlogObjectEncoder) add(attr slog.Attr) {
	*encoder.current = append(*encoder.current, attr)
}

func (encoder *zapSlogObjectEncoder) AddArray(key string, marshaler zapcore.ArrayMarshaler) error {
	array := &zapSlogArrayEncoder{}
	err := marshaler.MarshalLogArray(array)
	encoder.add(slog.Any(key, array.values))
	return err
}

func (encoder *zapSlogObjectEncoder) AddObject(key string, marshaler zapcore.ObjectMarshaler) error {
	object := newZapSlogObjectEncoder()
	err := marshaler.MarshalLogObject(object)
	encoder.add(slog.Attr{Key: key, Value: slog.GroupValue(object.slogAttrs()...)})
	return err
}

func (encoder *zapSlogObjectEncoder) AddBinary(key string, value []byte) {
	encoder.add(slog.Any(key, append([]byte(nil), value...)))
}

func (encoder *zapSlogObjectEncoder) AddByteString(key string, value []byte) {
	encoder.add(slog.String(key, string(value)))
}

func (encoder *zapSlogObjectEncoder) AddBool(key string, value bool) {
	encoder.add(slog.Bool(key, value))
}

func (encoder *zapSlogObjectEncoder) AddComplex128(key string, value complex128) {
	encoder.add(slog.String(key, fmt.Sprint(value)))
}

func (encoder *zapSlogObjectEncoder) AddComplex64(key string, value complex64) {
	encoder.add(slog.String(key, fmt.Sprint(value)))
}

func (encoder *zapSlogObjectEncoder) AddDuration(key string, value time.Duration) {
	encoder.add(slog.Duration(key, value))
}

func (encoder *zapSlogObjectEncoder) AddFloat64(key string, value float64) {
	encoder.add(slog.Float64(key, value))
}

func (encoder *zapSlogObjectEncoder) AddFloat32(key string, value float32) {
	encoder.add(slog.Float64(key, float64(value)))
}

func (encoder *zapSlogObjectEncoder) AddInt(key string, value int) {
	encoder.add(slog.Int(key, value))
}

func (encoder *zapSlogObjectEncoder) AddInt64(key string, value int64) {
	encoder.add(slog.Int64(key, value))
}

func (encoder *zapSlogObjectEncoder) AddInt32(key string, value int32) {
	encoder.add(slog.Int64(key, int64(value)))
}

func (encoder *zapSlogObjectEncoder) AddInt16(key string, value int16) {
	encoder.add(slog.Int64(key, int64(value)))
}

func (encoder *zapSlogObjectEncoder) AddInt8(key string, value int8) {
	encoder.add(slog.Int64(key, int64(value)))
}

func (encoder *zapSlogObjectEncoder) AddString(key string, value string) {
	encoder.add(slog.String(key, value))
}

func (encoder *zapSlogObjectEncoder) AddTime(key string, value time.Time) {
	encoder.add(slog.Time(key, value))
}

func (encoder *zapSlogObjectEncoder) AddUint(key string, value uint) {
	encoder.add(slog.Uint64(key, uint64(value)))
}

func (encoder *zapSlogObjectEncoder) AddUint64(key string, value uint64) {
	encoder.add(slog.Uint64(key, value))
}

func (encoder *zapSlogObjectEncoder) AddUint32(key string, value uint32) {
	encoder.add(slog.Uint64(key, uint64(value)))
}

func (encoder *zapSlogObjectEncoder) AddUint16(key string, value uint16) {
	encoder.add(slog.Uint64(key, uint64(value)))
}

func (encoder *zapSlogObjectEncoder) AddUint8(key string, value uint8) {
	encoder.add(slog.Uint64(key, uint64(value)))
}

func (encoder *zapSlogObjectEncoder) AddUintptr(key string, value uintptr) {
	encoder.add(slog.Uint64(key, uint64(value)))
}

func (encoder *zapSlogObjectEncoder) AddReflected(key string, value interface{}) error {
	encoder.add(slog.Any(key, value))
	return nil
}

func (encoder *zapSlogObjectEncoder) OpenNamespace(key string) {
	parent := encoder.current
	*parent = append(*parent, slog.Attr{Key: key})
	index := len(*parent) - 1
	child := []slog.Attr{}
	encoder.namespaces = append(encoder.namespaces, zapSlogNamespace{
		parent: parent,
		index:  index,
		child:  &child,
	})
	encoder.current = &child
}

func (encoder *zapSlogObjectEncoder) asMap() map[string]interface{} {
	attrs := encoder.slogAttrs()
	values := make(map[string]interface{}, len(attrs))
	for _, attr := range attrs {
		value := attr.Value.Resolve()
		if value.Kind() == slog.KindGroup {
			values[attr.Key] = slogAttrsAsMap(value.Group())
		} else {
			values[attr.Key] = value.Any()
		}
	}
	return values
}

func slogAttrsAsMap(attrs []slog.Attr) map[string]interface{} {
	values := make(map[string]interface{}, len(attrs))
	for _, attr := range attrs {
		value := attr.Value.Resolve()
		if value.Kind() == slog.KindGroup {
			values[attr.Key] = slogAttrsAsMap(value.Group())
		} else {
			values[attr.Key] = value.Any()
		}
	}
	return values
}

type zapSlogArrayEncoder struct {
	values []interface{}
}

func (encoder *zapSlogArrayEncoder) AppendBool(value bool) {
	encoder.values = append(encoder.values, value)
}

func (encoder *zapSlogArrayEncoder) AppendByteString(value []byte) {
	encoder.values = append(encoder.values, string(value))
}

func (encoder *zapSlogArrayEncoder) AppendComplex128(value complex128) {
	encoder.values = append(encoder.values, fmt.Sprint(value))
}

func (encoder *zapSlogArrayEncoder) AppendComplex64(value complex64) {
	encoder.values = append(encoder.values, fmt.Sprint(value))
}

func (encoder *zapSlogArrayEncoder) AppendFloat64(value float64) {
	encoder.values = append(encoder.values, value)
}

func (encoder *zapSlogArrayEncoder) AppendFloat32(value float32) {
	encoder.values = append(encoder.values, float64(value))
}

func (encoder *zapSlogArrayEncoder) AppendInt(value int) {
	encoder.values = append(encoder.values, value)
}

func (encoder *zapSlogArrayEncoder) AppendInt64(value int64) {
	encoder.values = append(encoder.values, value)
}

func (encoder *zapSlogArrayEncoder) AppendInt32(value int32) {
	encoder.values = append(encoder.values, int64(value))
}

func (encoder *zapSlogArrayEncoder) AppendInt16(value int16) {
	encoder.values = append(encoder.values, int64(value))
}

func (encoder *zapSlogArrayEncoder) AppendInt8(value int8) {
	encoder.values = append(encoder.values, int64(value))
}

func (encoder *zapSlogArrayEncoder) AppendString(value string) {
	encoder.values = append(encoder.values, value)
}

func (encoder *zapSlogArrayEncoder) AppendUint(value uint) {
	encoder.values = append(encoder.values, uint64(value))
}

func (encoder *zapSlogArrayEncoder) AppendUint64(value uint64) {
	encoder.values = append(encoder.values, value)
}

func (encoder *zapSlogArrayEncoder) AppendUint32(value uint32) {
	encoder.values = append(encoder.values, uint64(value))
}

func (encoder *zapSlogArrayEncoder) AppendUint16(value uint16) {
	encoder.values = append(encoder.values, uint64(value))
}

func (encoder *zapSlogArrayEncoder) AppendUint8(value uint8) {
	encoder.values = append(encoder.values, uint64(value))
}

func (encoder *zapSlogArrayEncoder) AppendUintptr(value uintptr) {
	encoder.values = append(encoder.values, uint64(value))
}

func (encoder *zapSlogArrayEncoder) AppendDuration(value time.Duration) {
	encoder.values = append(encoder.values, value)
}

func (encoder *zapSlogArrayEncoder) AppendTime(value time.Time) {
	encoder.values = append(encoder.values, value)
}

func (encoder *zapSlogArrayEncoder) AppendArray(marshaler zapcore.ArrayMarshaler) error {
	array := &zapSlogArrayEncoder{}
	err := marshaler.MarshalLogArray(array)
	encoder.values = append(encoder.values, array.values)
	return err
}

func (encoder *zapSlogArrayEncoder) AppendObject(marshaler zapcore.ObjectMarshaler) error {
	object := newZapSlogObjectEncoder()
	err := marshaler.MarshalLogObject(object)
	encoder.values = append(encoder.values, object.asMap())
	return err
}

func (encoder *zapSlogArrayEncoder) AppendReflected(value interface{}) error {
	encoder.values = append(encoder.values, value)
	return nil
}

var _ zapcore.ObjectEncoder = (*zapSlogObjectEncoder)(nil)
var _ zapcore.ArrayEncoder = (*zapSlogArrayEncoder)(nil)
var _ zapcore.Core = (*slogZapCore)(nil)

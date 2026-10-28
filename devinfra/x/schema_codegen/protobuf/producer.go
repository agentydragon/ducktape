package producer

import (
	"time"

	commonpb "github.com/agentydragon/ducktape/devinfra/x/schema_codegen/protobuf/commonpb"
	samplepb "github.com/agentydragon/ducktape/devinfra/x/schema_codegen/protobuf/samplepb"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
)

func Sample(producer string) *samplepb.Sample {
	return &samplepb.Sample{
		SampleId:   "sample-17",
		Sequence:   9_007_199_254_740_993,
		ObservedAt: timestamppb.New(time.Date(2026, time.December, 1, 12, 10, 45, 123_000_000, time.UTC)),
		Status:     commonpb.SampleStatus(37),
		Note:       proto.String(""),
		Metadata:   &commonpb.CommonMetadata{Producer: producer},
		Payload:    &samplepb.Sample_TextPayload{TextPayload: "protobuf across languages"},
	}
}

func ProtoJSON(producer string) ([]byte, error) {
	return protojson.Marshal(Sample(producer))
}

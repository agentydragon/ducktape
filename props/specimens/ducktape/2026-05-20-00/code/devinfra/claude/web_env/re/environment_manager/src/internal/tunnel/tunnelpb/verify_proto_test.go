// Test to verify the generated protobuf code has proper ProtoReflect implementation
package tunnelpb

import (
	"testing"
)

func TestProtoReflect(t *testing.T) {
	// Test that TunnelRequest has a proper ProtoReflect implementation
	req := &TunnelRequest{
		RequestId: "test-123",
		Path:      "/test",
		Method:    "GET",
	}

	if req.ProtoReflect() == nil {
		t.Error("TunnelRequest.ProtoReflect() returned nil - expected valid protoreflect.Message")
	}

	// Test TunnelResponse
	resp := &TunnelResponse{
		RequestId: "test-123",
		Status:    "200",
	}

	if resp.ProtoReflect() == nil {
		t.Error("TunnelResponse.ProtoReflect() returned nil - expected valid protoreflect.Message")
	}

	// Test nested message types
	cancel := &HttpCancel{
		RequestId: "test-123",
	}

	if cancel.ProtoReflect() == nil {
		t.Error("HttpCancel.ProtoReflect() returned nil - expected valid protoreflect.Message")
	}
}

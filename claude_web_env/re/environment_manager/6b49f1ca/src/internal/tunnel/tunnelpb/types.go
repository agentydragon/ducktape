// Reconstructed stub types for the tunnel protobuf package.
// Original import path: github.com/anthropics/anthropic/api-go/gen/proto/anthropic/sessions/tunnel/v1alpha
//
// These are plain Go structs that mirror the protobuf-generated types used by
// the tunnel client/handler code. They include no-op proto.Message method stubs
// and getter methods matching the protoc-gen-go conventions observed in the binary.
//
// Source: reverse-engineered from environment-manager binary (Build ID: 6b49f1ca)

package tunnelpb

// ---------------------------------------------------------------------------
// Header
// ---------------------------------------------------------------------------

type Header struct {
	Key   string `protobuf:"bytes,1,opt,name=key,proto3" json:"key,omitempty"`
	Value string `protobuf:"bytes,2,opt,name=value,proto3" json:"value,omitempty"`
}

func (x *Header) Reset()         {}
func (x *Header) String() string { return x.Key + ": " + x.Value }
func (x *Header) ProtoMessage()  {}

func (x *Header) GetKey() string {
	if x != nil {
		return x.Key
	}
	return ""
}

func (x *Header) GetValue() string {
	if x != nil {
		return x.Value
	}
	return ""
}

// ---------------------------------------------------------------------------
// TunnelRequest
// ---------------------------------------------------------------------------

// isTunnelRequestPayload is the oneof interface for TunnelRequest.Payload.
type isTunnelRequestPayload interface {
	isTunnelRequestPayload()
}

// TunnelRequest is the top-level message received from the tunnel server.
type TunnelRequest struct {
	RequestId   string                 `protobuf:"bytes,1,opt,name=request_id,json=requestId,proto3" json:"requestId,omitempty"`
	TunnelId    string                 `protobuf:"bytes,2,opt,name=tunnel_id,json=tunnelId,proto3" json:"tunnelId,omitempty"`
	Path        string                 `protobuf:"bytes,3,opt,name=path,proto3" json:"path,omitempty"`
	Method      string                 `protobuf:"bytes,4,opt,name=method,proto3" json:"method,omitempty"`
	Url         string                 `protobuf:"bytes,5,opt,name=url,proto3" json:"url,omitempty"`
	Port        int32                  `protobuf:"varint,6,opt,name=port,proto3" json:"port,omitempty"`
	ContentType string                 `protobuf:"bytes,7,opt,name=content_type,json=contentType,proto3" json:"contentType,omitempty"`
	Body        []byte                 `protobuf:"bytes,8,opt,name=body,proto3" json:"body,omitempty"`
	Headers     map[string][]string    `protobuf:"-" json:"headers,omitempty"`
	Payload     isTunnelRequestPayload `protobuf_oneof:"payload"`
}

func (x *TunnelRequest) Reset()         {}
func (x *TunnelRequest) String() string { return "TunnelRequest" }
func (x *TunnelRequest) ProtoMessage()  {}

func (x *TunnelRequest) GetPayload() isTunnelRequestPayload {
	if x != nil {
		return x.Payload
	}
	return nil
}

func (x *TunnelRequest) GetRequestId() string {
	if x != nil {
		return x.RequestId
	}
	return ""
}

func (x *TunnelRequest) GetTunnelId() string {
	if x != nil {
		return x.TunnelId
	}
	return ""
}

func (x *TunnelRequest) GetPath() string {
	if x != nil {
		return x.Path
	}
	return ""
}

func (x *TunnelRequest) GetMethod() string {
	if x != nil {
		return x.Method
	}
	return ""
}

func (x *TunnelRequest) GetUrl() string {
	if x != nil {
		return x.Url
	}
	return ""
}

func (x *TunnelRequest) GetPort() int32 {
	if x != nil {
		return x.Port
	}
	return 0
}

func (x *TunnelRequest) GetContentType() string {
	if x != nil {
		return x.ContentType
	}
	return ""
}

func (x *TunnelRequest) GetBody() []byte {
	if x != nil {
		return x.Body
	}
	return nil
}

func (x *TunnelRequest) GetHeaders() map[string][]string {
	if x != nil {
		return x.Headers
	}
	return nil
}

// ---------------------------------------------------------------------------
// TunnelRequest oneof payload variants
// ---------------------------------------------------------------------------

// TunnelRequest_HttpRequest wraps an HTTP request payload.
type TunnelRequest_HttpRequest struct {
	HttpRequest *TunnelRequest `protobuf:"bytes,10,opt,name=http_request,json=httpRequest,proto3,oneof"`
}

func (*TunnelRequest_HttpRequest) isTunnelRequestPayload() {}

func (x *TunnelRequest_HttpRequest) GetPath() string {
	if x != nil && x.HttpRequest != nil {
		return x.HttpRequest.Path
	}
	return ""
}

func (x *TunnelRequest_HttpRequest) GetPort() int32 {
	if x != nil && x.HttpRequest != nil {
		return x.HttpRequest.Port
	}
	return 0
}

func (x *TunnelRequest_HttpRequest) GetUrl() string {
	if x != nil && x.HttpRequest != nil {
		return x.HttpRequest.Url
	}
	return ""
}

func (x *TunnelRequest_HttpRequest) GetHeaders() map[string][]string {
	if x != nil && x.HttpRequest != nil {
		return x.HttpRequest.Headers
	}
	return nil
}

// TunnelRequest_HttpCancel wraps an HTTP cancel payload.
type TunnelRequest_HttpCancel struct {
	HttpCancel *HttpCancel `protobuf:"bytes,11,opt,name=http_cancel,json=httpCancel,proto3,oneof"`
}

func (*TunnelRequest_HttpCancel) isTunnelRequestPayload() {}

func (x *TunnelRequest_HttpCancel) GetRequestId() string {
	if x != nil && x.HttpCancel != nil {
		return x.HttpCancel.RequestId
	}
	return ""
}

// HttpCancel represents a cancellation of an in-flight HTTP request.
type HttpCancel struct {
	RequestId string `protobuf:"bytes,1,opt,name=request_id,json=requestId,proto3" json:"requestId,omitempty"`
}

func (x *HttpCancel) Reset()         {}
func (x *HttpCancel) String() string { return "HttpCancel" }
func (x *HttpCancel) ProtoMessage()  {}

func (x *HttpCancel) GetRequestId() string {
	if x != nil {
		return x.RequestId
	}
	return ""
}

// TunnelRequest_WsOpen wraps a WebSocket open payload.
type TunnelRequest_WsOpen struct {
	WsOpen *WsOpen `protobuf:"bytes,12,opt,name=ws_open,json=wsOpen,proto3,oneof"`
}

func (*TunnelRequest_WsOpen) isTunnelRequestPayload() {}

func (x *TunnelRequest_WsOpen) GetPath() string {
	if x != nil && x.WsOpen != nil {
		return x.WsOpen.Path
	}
	return ""
}

func (x *TunnelRequest_WsOpen) GetPort() int32 {
	if x != nil && x.WsOpen != nil {
		return x.WsOpen.Port
	}
	return 0
}

func (x *TunnelRequest_WsOpen) GetUrl() string {
	if x != nil && x.WsOpen != nil {
		return x.WsOpen.Url
	}
	return ""
}

func (x *TunnelRequest_WsOpen) GetHeaders() map[string][]string {
	if x != nil && x.WsOpen != nil {
		return x.WsOpen.Headers
	}
	return nil
}

// WsOpen represents a request to open a WebSocket tunnel connection.
type WsOpen struct {
	Path         string              `protobuf:"bytes,1,opt,name=path,proto3" json:"path,omitempty"`
	Port         int32               `protobuf:"varint,2,opt,name=port,proto3" json:"port,omitempty"`
	Url          string              `protobuf:"bytes,3,opt,name=url,proto3" json:"url,omitempty"`
	ConnectionId string              `protobuf:"bytes,4,opt,name=connection_id,json=connectionId,proto3" json:"connectionId,omitempty"`
	Headers      map[string][]string `protobuf:"-" json:"headers,omitempty"`
}

func (x *WsOpen) Reset()         {}
func (x *WsOpen) String() string { return "WsOpen" }
func (x *WsOpen) ProtoMessage()  {}

func (x *WsOpen) GetPath() string {
	if x != nil {
		return x.Path
	}
	return ""
}

func (x *WsOpen) GetPort() int32 {
	if x != nil {
		return x.Port
	}
	return 0
}

func (x *WsOpen) GetUrl() string {
	if x != nil {
		return x.Url
	}
	return ""
}

func (x *WsOpen) GetConnectionId() string {
	if x != nil {
		return x.ConnectionId
	}
	return ""
}

func (x *WsOpen) GetHeaders() map[string][]string {
	if x != nil {
		return x.Headers
	}
	return nil
}

// TunnelRequest_WsMessage wraps a WebSocket message payload.
type TunnelRequest_WsMessage struct {
	WsMessage *WsMessage `protobuf:"bytes,13,opt,name=ws_message,json=wsMessage,proto3,oneof"`
}

func (*TunnelRequest_WsMessage) isTunnelRequestPayload() {}

func (x *TunnelRequest_WsMessage) GetConnectionId() string {
	if x != nil && x.WsMessage != nil {
		return x.WsMessage.ConnectionId
	}
	return ""
}

func (x *TunnelRequest_WsMessage) GetType() int32 {
	if x != nil && x.WsMessage != nil {
		return x.WsMessage.Type
	}
	return 0
}

func (x *TunnelRequest_WsMessage) GetData() []byte {
	if x != nil && x.WsMessage != nil {
		return x.WsMessage.Data
	}
	return nil
}

// TunnelRequest_WsClose wraps a WebSocket close payload.
type TunnelRequest_WsClose struct {
	WsClose *WsClose `protobuf:"bytes,14,opt,name=ws_close,json=wsClose,proto3,oneof"`
}

func (*TunnelRequest_WsClose) isTunnelRequestPayload() {}

func (x *TunnelRequest_WsClose) GetPath() string {
	if x != nil && x.WsClose != nil {
		return x.WsClose.Path
	}
	return ""
}

func (x *TunnelRequest_WsClose) GetPort() int32 {
	if x != nil && x.WsClose != nil {
		return x.WsClose.Port
	}
	return 0
}

func (x *TunnelRequest_WsClose) GetUrl() string {
	if x != nil && x.WsClose != nil {
		return x.WsClose.Url
	}
	return ""
}

func (x *TunnelRequest_WsClose) GetConnectionId() string {
	if x != nil && x.WsClose != nil {
		return x.WsClose.ConnectionId
	}
	return ""
}

func (x *TunnelRequest_WsClose) GetReason() string {
	if x != nil && x.WsClose != nil {
		return x.WsClose.Reason
	}
	return ""
}

// ---------------------------------------------------------------------------
// TunnelResponse
// ---------------------------------------------------------------------------

// isTunnelResponsePayload is the oneof interface for TunnelResponse.Payload.
type isTunnelResponsePayload interface {
	isTunnelResponsePayload()
}

// TunnelResponse is the top-level message sent back through the tunnel.
type TunnelResponse struct {
	RequestId   string                  `protobuf:"bytes,1,opt,name=request_id,json=requestId,proto3" json:"requestId,omitempty"`
	Status      string                  `protobuf:"bytes,2,opt,name=status,proto3" json:"status,omitempty"`
	ContentType string                  `protobuf:"bytes,3,opt,name=content_type,json=contentType,proto3" json:"contentType,omitempty"`
	Body        []byte                  `protobuf:"bytes,4,opt,name=body,proto3" json:"body,omitempty"`
	Streaming   bool                    `protobuf:"varint,5,opt,name=streaming,proto3" json:"streaming,omitempty"`
	Payload     isTunnelResponsePayload `protobuf_oneof:"payload"`
}

func (x *TunnelResponse) Reset()         {}
func (x *TunnelResponse) String() string { return "TunnelResponse" }
func (x *TunnelResponse) ProtoMessage()  {}

func (x *TunnelResponse) GetRequestId() string {
	if x != nil {
		return x.RequestId
	}
	return ""
}

func (x *TunnelResponse) GetStatus() string {
	if x != nil {
		return x.Status
	}
	return ""
}

func (x *TunnelResponse) GetContentType() string {
	if x != nil {
		return x.ContentType
	}
	return ""
}

func (x *TunnelResponse) GetBody() []byte {
	if x != nil {
		return x.Body
	}
	return nil
}

func (x *TunnelResponse) GetStreaming() bool {
	if x != nil {
		return x.Streaming
	}
	return false
}

func (x *TunnelResponse) GetPayload() isTunnelResponsePayload {
	if x != nil {
		return x.Payload
	}
	return nil
}

// ---------------------------------------------------------------------------
// TunnelResponse oneof payload variants
// ---------------------------------------------------------------------------

type TunnelResponse_HttpHeaders struct {
	HttpHeaders *HttpHeaders `protobuf:"bytes,10,opt,name=http_headers,json=httpHeaders,proto3,oneof"`
}

func (*TunnelResponse_HttpHeaders) isTunnelResponsePayload() {}

type TunnelResponse_HttpChunk struct {
	HttpChunk *HttpChunk `protobuf:"bytes,11,opt,name=http_chunk,json=httpChunk,proto3,oneof"`
}

func (*TunnelResponse_HttpChunk) isTunnelResponsePayload() {}

type TunnelResponse_HttpError struct {
	HttpError *HttpError `protobuf:"bytes,12,opt,name=http_error,json=httpError,proto3,oneof"`
}

func (*TunnelResponse_HttpError) isTunnelResponsePayload() {}

type TunnelResponse_WsOpened struct {
	WsOpened *WsOpened `protobuf:"bytes,13,opt,name=ws_opened,json=wsOpened,proto3,oneof"`
}

func (*TunnelResponse_WsOpened) isTunnelResponsePayload() {}

type TunnelResponse_WsMessage struct {
	WsMessage *WsMessage `protobuf:"bytes,14,opt,name=ws_message,json=wsMessage,proto3,oneof"`
}

func (*TunnelResponse_WsMessage) isTunnelResponsePayload() {}

type TunnelResponse_WsClose struct {
	WsClose *WsClose `protobuf:"bytes,15,opt,name=ws_close,json=wsClose,proto3,oneof"`
}

func (*TunnelResponse_WsClose) isTunnelResponsePayload() {}

type TunnelResponse_WsError struct {
	WsError *WsError `protobuf:"bytes,16,opt,name=ws_error,json=wsError,proto3,oneof"`
}

func (*TunnelResponse_WsError) isTunnelResponsePayload() {}

// ---------------------------------------------------------------------------
// HTTP response message types
// ---------------------------------------------------------------------------

// HttpHeaders represents the initial HTTP response headers sent through the tunnel.
type HttpHeaders struct {
	StatusCode int32     `protobuf:"varint,1,opt,name=status_code,json=statusCode,proto3" json:"statusCode,omitempty"`
	Headers    []*Header `protobuf:"bytes,2,rep,name=headers,proto3" json:"headers,omitempty"`
}

func (x *HttpHeaders) Reset()         {}
func (x *HttpHeaders) String() string { return "HttpHeaders" }
func (x *HttpHeaders) ProtoMessage()  {}

func (x *HttpHeaders) GetStatusCode() int32 {
	if x != nil {
		return x.StatusCode
	}
	return 0
}

func (x *HttpHeaders) GetHeaders() []*Header {
	if x != nil {
		return x.Headers
	}
	return nil
}

// HttpChunk represents a chunk of HTTP response body data.
type HttpChunk struct {
	Data []byte `protobuf:"bytes,1,opt,name=data,proto3" json:"data,omitempty"`
}

func (x *HttpChunk) Reset()         {}
func (x *HttpChunk) String() string { return "HttpChunk" }
func (x *HttpChunk) ProtoMessage()  {}

func (x *HttpChunk) GetData() []byte {
	if x != nil {
		return x.Data
	}
	return nil
}

// HttpError represents an HTTP error response.
type HttpError struct {
	Message    string `protobuf:"bytes,1,opt,name=message,proto3" json:"message,omitempty"`
	StatusCode int32  `protobuf:"varint,2,opt,name=status_code,json=statusCode,proto3" json:"statusCode,omitempty"`
}

func (x *HttpError) Reset()         {}
func (x *HttpError) String() string { return "HttpError" }
func (x *HttpError) ProtoMessage()  {}

func (x *HttpError) GetMessage() string {
	if x != nil {
		return x.Message
	}
	return ""
}

func (x *HttpError) GetStatusCode() int32 {
	if x != nil {
		return x.StatusCode
	}
	return 0
}

// ---------------------------------------------------------------------------
// WebSocket message types
// ---------------------------------------------------------------------------

// WsOpened signals that a WebSocket tunnel connection was successfully opened.
type WsOpened struct{}

func (x *WsOpened) Reset()         {}
func (x *WsOpened) String() string { return "WsOpened" }
func (x *WsOpened) ProtoMessage()  {}

// WsMessage represents a WebSocket message forwarded through the tunnel.
type WsMessage struct {
	Data         []byte `protobuf:"bytes,1,opt,name=data,proto3" json:"data,omitempty"`
	Type         int32  `protobuf:"varint,2,opt,name=type,proto3" json:"type,omitempty"`
	ConnectionId string `protobuf:"bytes,3,opt,name=connection_id,json=connectionId,proto3" json:"connectionId,omitempty"`
}

func (x *WsMessage) Reset()         {}
func (x *WsMessage) String() string { return "WsMessage" }
func (x *WsMessage) ProtoMessage()  {}

func (x *WsMessage) GetData() []byte {
	if x != nil {
		return x.Data
	}
	return nil
}

func (x *WsMessage) GetType() int32 {
	if x != nil {
		return x.Type
	}
	return 0
}

func (x *WsMessage) GetConnectionId() string {
	if x != nil {
		return x.ConnectionId
	}
	return ""
}

// WsClose represents a WebSocket close frame forwarded through the tunnel.
type WsClose struct {
	Code         int32  `protobuf:"varint,1,opt,name=code,proto3" json:"code,omitempty"`
	Reason       string `protobuf:"bytes,2,opt,name=reason,proto3" json:"reason,omitempty"`
	Path         string `protobuf:"bytes,3,opt,name=path,proto3" json:"path,omitempty"`
	Port         int32  `protobuf:"varint,4,opt,name=port,proto3" json:"port,omitempty"`
	Url          string `protobuf:"bytes,5,opt,name=url,proto3" json:"url,omitempty"`
	ConnectionId string `protobuf:"bytes,6,opt,name=connection_id,json=connectionId,proto3" json:"connectionId,omitempty"`
}

func (x *WsClose) Reset()         {}
func (x *WsClose) String() string { return "WsClose" }
func (x *WsClose) ProtoMessage()  {}

func (x *WsClose) GetCode() int32 {
	if x != nil {
		return x.Code
	}
	return 0
}

func (x *WsClose) GetReason() string {
	if x != nil {
		return x.Reason
	}
	return ""
}

func (x *WsClose) GetPath() string {
	if x != nil {
		return x.Path
	}
	return ""
}

func (x *WsClose) GetPort() int32 {
	if x != nil {
		return x.Port
	}
	return 0
}

func (x *WsClose) GetUrl() string {
	if x != nil {
		return x.Url
	}
	return ""
}

func (x *WsClose) GetConnectionId() string {
	if x != nil {
		return x.ConnectionId
	}
	return ""
}

// WsError represents a WebSocket error.
type WsError struct {
	Message string `protobuf:"bytes,1,opt,name=message,proto3" json:"message,omitempty"`
}

func (x *WsError) Reset()         {}
func (x *WsError) String() string { return "WsError" }
func (x *WsError) ProtoMessage()  {}

func (x *WsError) GetMessage() string {
	if x != nil {
		return x.Message
	}
	return ""
}

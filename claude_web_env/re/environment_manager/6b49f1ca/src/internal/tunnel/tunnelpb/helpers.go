// Custom helpers and extensions for the generated tunnel protobuf code.
// These provide compatibility with the original stub implementation.

package tunnelpb

// GetHeaders returns the headers map for TunnelRequest.
// This is a custom field not directly supported in proto3 (map with repeated values).
// The actual wire format would serialize this differently.
func (x *TunnelRequest) GetHeaders() map[string][]string {
	if x != nil {
		// In the actual implementation, this would be populated from the wire format
		// For now, return empty map to match the stub behavior
		return make(map[string][]string)
	}
	return nil
}

// Helper methods for TunnelRequest_HttpCancel wrapper

func (x *TunnelRequest_HttpCancel) GetRequestId() string {
	if x != nil && x.HttpCancel != nil {
		return x.HttpCancel.RequestId
	}
	return ""
}

// Helper methods for TunnelRequest_WsOpen wrapper

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
		return x.WsOpen.GetHeaders()
	}
	return nil
}

// GetHeaders returns the headers map for WsOpen.
// This is a custom field not directly supported in proto3.
func (x *WsOpen) GetHeaders() map[string][]string {
	if x != nil {
		return make(map[string][]string)
	}
	return nil
}

// Helper methods for TunnelRequest_WsMessage wrapper

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

// Helper methods for TunnelRequest_WsClose wrapper

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

// Helper methods for TunnelRequest_HttpRequest wrapper

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
		return x.HttpRequest.GetHeaders()
	}
	return nil
}

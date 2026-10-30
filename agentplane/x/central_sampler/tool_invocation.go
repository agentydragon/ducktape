package centralsampler

import "github.com/agentydragon/ducktape/agentplane/x/central_sampler/protocol"

func unsupportedToolCallMessage(call protocol.ToolInvocation) string {
	if call.Encoding == protocol.ExecEncodingFreeform {
		return "unsupported custom tool call: " + call.Name
	}
	return "unsupported call: " + call.Name
}

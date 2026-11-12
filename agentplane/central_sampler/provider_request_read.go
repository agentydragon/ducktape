package centralsampler

import "strings"

const maxProviderRequestBodyChunkSize = 1 << 20

func nonAuthProviderRequestHeaders(headers map[string]string) map[string]string {
	result := make(map[string]string, len(headers))
	for name, value := range headers {
		switch strings.ToLower(name) {
		case "authorization", "proxy-authorization", "x-api-key", "api-key", "x-auth-token", "x-access-token":
			continue
		}
		result[name] = value
	}
	return result
}

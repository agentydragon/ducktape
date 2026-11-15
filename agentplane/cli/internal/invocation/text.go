package invocation

import "strings"

func Fields(values ...string) string {
	return strings.Join(values, "  ")
}

func FirstLine(text string) string {
	line, _, _ := strings.Cut(strings.TrimSpace(text), "\n")
	return line
}

func EnumSuffix(name, prefix string) string {
	return strings.ToLower(strings.TrimPrefix(name, prefix))
}

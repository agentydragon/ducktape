package mcpserver

import "strconv"

func seconds(value float64) string {
	return strconv.FormatFloat(value, 'g', -1, 64)
}

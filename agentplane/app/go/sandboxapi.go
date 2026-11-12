package main

import "strings"

func quote(value string) string { return "'" + strings.ReplaceAll(value, "'", "\\'") + "'" }

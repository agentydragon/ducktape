// Package frontend contains the platform sessions owner UI.
package frontend

import _ "embed"

//go:embed status.html
var HTML []byte

//go:embed status.css
var CSS []byte

//go:embed status.js
var JavaScript []byte

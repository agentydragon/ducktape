package mcpserver

import (
	"fmt"
	"math"
	"slices"
	"strconv"
	"strings"
	"unicode"
	"unicode/utf8"

	"google.golang.org/protobuf/types/known/structpb"
)

// jsonObject is a JSON object whose members keep their order: the field order of the Python model it stands for.
type jsonObject []jsonMember

type jsonMember struct {
	key   string
	value any
}

// jsonStyle is one of the two ways the Python service writes JSON into a tool answer. The values it encodes are nil,
// bool, string, int64, float64, jsonObject, []any, []string and the free-form *structpb.Value and *structpb.Struct.
type jsonStyle struct {
	itemSeparator string
	keySeparator  string
	asciiOnly     bool
	float         func(float64) string
}

var (
	// pydanticJSON is how Pydantic serializes a structured answer into its text block.
	pydanticJSON = jsonStyle{itemSeparator: ",", keySeparator: ":", float: pydanticFloat}
	// pythonDumps is Python's `json.dumps` with its defaults, which error texts embed.
	pythonDumps = jsonStyle{itemSeparator: ", ", keySeparator: ": ", asciiOnly: true, float: pythonFloatRepr}
)

func (s jsonStyle) encode(value any) string {
	var out strings.Builder
	s.write(&out, value)
	return out.String()
}

func (s jsonStyle) write(out *strings.Builder, value any) {
	switch value := value.(type) {
	case nil:
		out.WriteString("null")
	case bool:
		out.WriteString(strconv.FormatBool(value))
	case string:
		s.writeString(out, value)
	case int64:
		out.WriteString(strconv.FormatInt(value, 10))
	case float64:
		out.WriteString(s.float(value))
	case jsonObject:
		out.WriteByte('{')
		for i, member := range value {
			if i > 0 {
				out.WriteString(s.itemSeparator)
			}
			s.writeString(out, member.key)
			out.WriteString(s.keySeparator)
			s.write(out, member.value)
		}
		out.WriteByte('}')
	case []any:
		out.WriteByte('[')
		for i, item := range value {
			if i > 0 {
				out.WriteString(s.itemSeparator)
			}
			s.write(out, item)
		}
		out.WriteByte(']')
	case []string:
		out.WriteByte('[')
		for i, item := range value {
			if i > 0 {
				out.WriteString(s.itemSeparator)
			}
			s.writeString(out, item)
		}
		out.WriteByte(']')
	case *structpb.Struct:
		s.write(out, freeFormObject(value))
	case *structpb.Value:
		s.write(out, freeForm(value))
	default:
		panic(fmt.Sprintf("mcpserver: %T has no JSON form", value))
	}
}

func (s jsonStyle) writeString(out *strings.Builder, text string) {
	out.WriteByte('"')
	for _, r := range text {
		switch r {
		case '"':
			out.WriteString(`\"`)
		case '\\':
			out.WriteString(`\\`)
		case '\n':
			out.WriteString(`\n`)
		case '\r':
			out.WriteString(`\r`)
		case '\t':
			out.WriteString(`\t`)
		case '\b':
			out.WriteString(`\b`)
		case '\f':
			out.WriteString(`\f`)
		default:
			switch {
			case r < 0x20 || (s.asciiOnly && r > '~' && r <= 0xffff):
				fmt.Fprintf(out, `\u%04x`, r)
			case s.asciiOnly && r > 0xffff:
				high, low := utf16Surrogates(r)
				fmt.Fprintf(out, `\u%04x\u%04x`, high, low)
			default:
				out.WriteRune(r)
			}
		}
	}
	out.WriteByte('"')
}

func utf16Surrogates(r rune) (rune, rune) {
	r -= 0x10000
	return 0xd800 + (r>>10)&0x3ff, 0xdc00 + r&0x3ff
}

// freeForm is a stored JSON value as Python reads it back from PostgreSQL: object members in JSONB's key order, and
// an integral number as an integer.
func freeForm(value *structpb.Value) any {
	switch kind := value.GetKind().(type) {
	case *structpb.Value_NullValue:
		return nil
	case *structpb.Value_BoolValue:
		return kind.BoolValue
	case *structpb.Value_StringValue:
		return kind.StringValue
	case *structpb.Value_NumberValue:
		return freeFormNumber(kind.NumberValue)
	case *structpb.Value_StructValue:
		return freeFormObject(kind.StructValue)
	case *structpb.Value_ListValue:
		items := make([]any, 0, len(kind.ListValue.GetValues()))
		for _, item := range kind.ListValue.GetValues() {
			items = append(items, freeForm(item))
		}
		return items
	default:
		// An unset Value is what protojson reads for JSON null inside a list or object.
		return nil
	}
}

// maxExactInteger is the largest magnitude below which every integer is a float64.
const maxExactInteger = 1 << 53

func freeFormNumber(number float64) any {
	if number == math.Trunc(number) && math.Abs(number) <= maxExactInteger {
		return int64(number)
	}
	return number
}

func freeFormObject(object *structpb.Struct) jsonObject {
	keys := make([]string, 0, len(object.GetFields()))
	for key := range object.GetFields() {
		keys = append(keys, key)
	}
	slices.SortFunc(keys, jsonbKeyOrder)
	members := make(jsonObject, 0, len(keys))
	for _, key := range keys {
		members = append(members, jsonMember{key: key, value: freeForm(object.GetFields()[key])})
	}
	return members
}

// jsonbKeyOrder is the order PostgreSQL stores a JSONB object's keys in: shorter first, then bytewise.
func jsonbKeyOrder(a, b string) int {
	if len(a) != len(b) {
		return len(a) - len(b)
	}
	return strings.Compare(a, b)
}

// shortestDigits is the shortest decimal that reads back as |x|: its digits, and the power of ten of the first.
func shortestDigits(x float64) (string, int) {
	mantissa, exponent, _ := strings.Cut(strconv.FormatFloat(math.Abs(x), 'e', -1, 64), "e")
	power, err := strconv.Atoi(exponent)
	if err != nil {
		panic(fmt.Sprintf("mcpserver: strconv wrote exponent %q", exponent))
	}
	return strings.Replace(mantissa, ".", "", 1), power
}

// formatFloat writes x as Python-family serializers do: positional between 10**lowest and 10**16 with at least one
// fractional digit, scientific otherwise.
func formatFloat(x float64, lowest int, padExponent bool) string {
	sign := ""
	if math.Signbit(x) {
		sign = "-"
	}
	if x == 0 {
		return sign + "0.0"
	}
	digits, power := shortestDigits(x)
	if power < lowest || power >= 16 {
		mantissa := digits[:1]
		if len(digits) > 1 {
			mantissa += "." + digits[1:]
		}
		exponentSign := "+"
		if power < 0 {
			exponentSign = "-"
		}
		exponent := strconv.Itoa(abs(power))
		if padExponent && len(exponent) < 2 {
			exponent = "0" + exponent
		}
		return sign + mantissa + "e" + exponentSign + exponent
	}
	if power < 0 {
		return sign + "0." + strings.Repeat("0", -power-1) + digits
	}
	if len(digits) <= power+1 {
		return sign + digits + strings.Repeat("0", power+1-len(digits)) + ".0"
	}
	return sign + digits[:power+1] + "." + digits[power+1:]
}

func abs(n int) int {
	if n < 0 {
		return -n
	}
	return n
}

// pythonFloatRepr is Python's `repr` of a float, which `json.dumps` writes.
func pythonFloatRepr(x float64) string { return formatFloat(x, -4, true) }

// pydanticFloat is how Pydantic's JSON serializer writes a float.
func pydanticFloat(x float64) string { return formatFloat(x, -5, false) }

// pythonG is Python's `format(x, "g")`.
func pythonG(x float64) string { return strconv.FormatFloat(x, 'g', 6, 64) }

// pythonRepr is Python's `repr` of a string.
func pythonRepr(text string) string {
	quote := '\''
	if strings.ContainsRune(text, '\'') && !strings.ContainsRune(text, '"') {
		quote = '"'
	}
	var out strings.Builder
	out.WriteRune(quote)
	for _, r := range text {
		switch {
		case r == quote || r == '\\':
			out.WriteByte('\\')
			out.WriteRune(r)
		case r == '\t':
			out.WriteString(`\t`)
		case r == '\n':
			out.WriteString(`\n`)
		case r == '\r':
			out.WriteString(`\r`)
		case r == ' ' || (r < utf8.RuneSelf && r > ' ' && r < 0x7f):
			out.WriteRune(r)
		case r >= utf8.RuneSelf && unicode.IsGraphic(r) && !unicode.Is(unicode.Zs, r):
			out.WriteRune(r)
		case r <= 0xff:
			fmt.Fprintf(&out, `\x%02x`, r)
		case r <= 0xffff:
			fmt.Fprintf(&out, `\u%04x`, r)
		default:
			fmt.Fprintf(&out, `\U%08x`, r)
		}
	}
	out.WriteRune(quote)
	return out.String()
}

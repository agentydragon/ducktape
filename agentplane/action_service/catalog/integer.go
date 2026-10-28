package catalog

import (
	"bytes"
	"encoding/json"
	"errors"
	"math"
	"math/big"
	"strconv"
	"strings"

	"github.com/invopop/jsonschema"
)

// Integer is a JSON Schema integer on the wire, including integral decimal and exponent
// spellings such as 60.0 and 6e1. Its Go representation is exact signed 64-bit integer.
type Integer int64

func (Integer) JSONSchema() *jsonschema.Schema {
	return &jsonschema.Schema{
		Type: "integer", Minimum: json.Number(strconv.FormatInt(math.MinInt64, 10)),
		Maximum: json.Number(strconv.FormatInt(math.MaxInt64, 10)),
	}
}

// UnmarshalJSON accepts exactly the integral JSON numbers in the signed 64-bit range. UseNumber
// keeps the original decimal spelling; big.Rat checks integrality without float64 rounding.
func (integer *Integer) UnmarshalJSON(data []byte) error {
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.UseNumber()
	var value any
	if err := decoder.Decode(&value); err != nil {
		return err
	}
	number, ok := value.(json.Number)
	if !ok || !json.Valid(data) {
		return errors.New("expected an integer JSON number")
	}
	spelling := number.String()
	// An exponent far larger than the literal's mantissa cannot yield a nonzero
	// signed 64-bit integer. Avoid asking big.Rat to allocate a huge power of ten.
	if exponentAt := strings.IndexAny(spelling, "eE"); exponentAt >= 0 {
		mantissa, valid := new(big.Rat).SetString(spelling[:exponentAt])
		if !valid {
			return errors.New("invalid integer JSON number")
		}
		if mantissa.Sign() == 0 {
			*integer = 0
			return nil
		}
		exponent, err := strconv.ParseInt(spelling[exponentAt+1:], 10, 64)
		if err != nil || exponent > int64(len(spelling))+19 || exponent < -int64(len(spelling))-19 {
			return errors.New("integer JSON number is out of range")
		}
	}
	rational, valid := new(big.Rat).SetString(spelling)
	if !valid || !rational.IsInt() || !rational.Num().IsInt64() {
		return errors.New("expected a signed 64-bit integer JSON number")
	}
	*integer = Integer(rational.Num().Int64())
	return nil
}

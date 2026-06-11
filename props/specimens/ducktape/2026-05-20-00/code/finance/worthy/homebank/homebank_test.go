package homebank

import "testing"

const CzechDateFormat = "02.01.2006"

func TestDateParsing(t *testing.T) {
	assertParsesTo := func(index int, date string) {
		actual := ParseHomebankDate(index).Format(CzechDateFormat)
		if actual != date {
			t.Errorf("%v should parse to %v, but parses to %v", index, actual, date)
		}
	}

	assertParsesTo(735957, "25.12.2015")
	assertParsesTo(735962, "30.12.2015")
	assertParsesTo(735963, "31.12.2015")
}

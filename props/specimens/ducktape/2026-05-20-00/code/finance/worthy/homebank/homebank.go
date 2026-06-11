// Package homebank can parse some parts of Homebank XML files and can calculate
// the current balance of simple accounts.
//
// homebank, err := homebank.ParseHomebankFile("/home/me/homebank.xhb")
//
//	if err != nil {
//	  panic(err)
//	}
//
// total := float64(0)
//
//	for _, account := range homebank.Accounts {
//	  if account.Flags != nil && (*account.Flags)&homebank.FLAG_CLOSED == homebank.FLAG_CLOSED {
//	    // Skip closed accounts.
//	    continue
//	  }
//	  if account.Type == homebank.ASSETS_ACCOUNT {
//	    continue
//	  }
//	  fmt.Println(homebank.GetAccountBalance(account.Key))
//	}
package homebank

// TODO: Make the format more complete?

import (
	"encoding/xml"
	"io/ioutil"
	"os"
	"strconv"
	"time"
)

// Values of the Type field
const ASSETS_ACCOUNT = 3

// Closed account
const FLAG_CLOSED = 2

// Flag on take side
const FLAG_TAKE_SIDE = 2

const (
	PAYMODE_CC            = 3
	PAYMODE_CROSSTRANSFER = 5
)

type Account struct {
	Key     int     `xml:"key,attr"`
	Flags   *int    `xml:"flags,attr"`
	Pos     int     `xml:"pos,attr"`
	Type    int     `xml:"type,attr"`
	Name    string  `xml:"name,attr"`
	Initial float64 `xml:"initial,attr"`
	Minimum int     `xml:"minimum,attr"`
}

const OPERATION_RECONCILED = 2

type Operation struct {
	XMLName xml.Name `xml:"ope"`

	Date       int     `xml:"date,attr"`
	Amount     float64 `xml:"amount,attr"`
	Account    int     `xml:"account,attr"`
	DstAccount *int    `xml:"dst_account,attr"`
	Paymode    *int    `xml:"paymode,attr"`
	Status     *int    `xml:"st,attr"`
	Flags      *int    `xml:"flags,attr"`
	Payee      *int    `xml:"payee,attr"`
	Category   *int    `xml:"category,attr"`
	Wording    *string `xml:"wording,attr"`
	Info       *string `xml:"info,attr"`
	Kxfer      *int    `xml:"kxfer,attr"`
}

type Category struct {
	Key     int      `xml:"key,attr"`
	Parent  *int     `xml:"parent,attr"`
	Flags   *int     `xml:"flags,attr"`
	Name    string   `xml:"name,attr"`
	Budget0 *float64 `xml:"b0,attr"`
}

type Tag struct {
	Key  int    `xml:"key,attr"`
	Name string `xml:"name,attr"`
}

type Payee struct {
	Key  int    `xml:"key,attr"`
	Name string `xml:"name,attr"`
}

type Properties struct {
	Title       string `xml:"title,attr"`
	AutoSmode   int    `xml:"auto_smode,attr"`
	AutoWeekday int    `xml:"auto_weekday,attr"`
}

type Assignment struct {
	Key      int    `xml:"key,attr"`
	Flags    int    `xml:"flags,attr"`
	Name     string `xml:"name,attr"`
	Category int    `xml:"category,attr"`
}

type HomebankFile struct {
	XMLName xml.Name `xml:"homebank"`

	Properties Properties `xml:"properties"`

	Version string `xml:"v,attr"`
	D       string `xml:"d,attr"` // TODO: what's this?
	// TODO: <properties>
	// TODO: all other tags in the XML
	Accounts    []Account    `xml:"account"`
	Payees      []Payee      `xml:"pay"`
	Categories  []Category   `xml:"cat"`
	Tags        []Tag        `xml:"tag"`
	Assignments []Assignment `xml:"asg"`
	Operations  []Operation  `xml:"ope"`
}

func ParseHomebankDate(x int) time.Time {
	// Friday 2015-12-25 == 735957
	// TODO: check this better
	referencePoint := time.Date(2015, time.December, 25, 12, 0, 0, 0, time.UTC)
	referenceI := 735957

	// TODO: 735962 == 2015-12-30
	// TODO: 735963 == 2015-12-31

	delta := time.Duration(24*(x-referenceI)) * time.Hour
	date := referencePoint.Add(delta)
	return date
}

func DateToHomebank(x time.Time) int {
	// Friday 2015-12-25 == 2290681
	// TODO: check this better

	x = time.Date(x.Year(), x.Month(), x.Day(), 12, 0, 0, 0, time.UTC)

	referencePoint := time.Date(2015, time.December, 25, 12, 0, 0, 0, time.UTC)
	referenceI := 735957

	delta := x.Sub(referencePoint)
	daysElapsed := int(delta / (time.Hour * 24))

	homebankDate := daysElapsed + referenceI

	CzechDate := "02.01.2006"
	if ParseHomebankDate(homebankDate).Format(CzechDate) != x.Format(CzechDate) {
		panic("fail in settlement: " + x.Format(CzechDate) + " => " + strconv.Itoa(homebankDate) + " => " + ParseHomebankDate(homebankDate).Format(CzechDate))
	}

	return homebankDate
}

func (homebank *HomebankFile) GetAccountOperations(accountId int) []Operation {
	operations := make([]Operation, 0)
	for _, operation := range homebank.Operations {
		if operation.Account != accountId {
			continue
		}
		operations = append(operations, operation)
	}
	return operations
}

func (homebank *HomebankFile) GetAccountBalance(accountId int) float64 {
	var balance float64 = -1
	for _, account := range homebank.Accounts {
		if account.Key == accountId {
			balance = account.Initial
		}
	}
	if balance == -1 {
		panic("account not found")
	}

	for _, operation := range homebank.Operations {
		if operation.Account != accountId {
			continue
		}

		balance += operation.Amount
	}
	return balance
}

func ParseHomebankFile(path string) (result HomebankFile, err error) {
	bytes, err := ioutil.ReadFile(path)
	if err != nil {
		return result, err
	}
	err = xml.Unmarshal(bytes, &result)
	return result, err
}

func (operation Operation) GetXml() string {
	bytes, err := xml.Marshal(operation)
	if err != nil {
		panic(err)
	}
	return string(bytes)
}

func WriteHomebankFile(homebank HomebankFile, path string) {
	bytes, err := xml.MarshalIndent(homebank, " ", "")
	if err != nil {
		panic(err)
	}
	if err := os.WriteFile(path, bytes, 0o644); err != nil {
		panic(err)
	}
}

/*
func main() {
	result, err := ParseHomebankFile("/home/prvak/dropbox/finance/ucetnictvi.xhb")
	if err != nil {
		panic(err)
	}

	total := float64(0)
	for _, account := range(result.Accounts) {
		if account.Flags & FLAG_CLOSED == FLAG_CLOSED {
			// Skip closed accounts.
			continue
		}
		if account.Type == ASSETS_ACCOUNT {
			// We manage assets accounts ourselves.
			continue
		}
		fmt.Println(account)
		fmt.Println(result.GetAccountBalance(account.Key))
		fmt.Println()
	}

	fmt.Printf("Version: %v\n", result.Version)
	fmt.Println(total)
}
*/

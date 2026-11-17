package store

import "strconv"

type argList []any

func (a *argList) add(value any) string {
	*a = append(*a, value)
	return "$" + strconv.Itoa(len(*a))
}

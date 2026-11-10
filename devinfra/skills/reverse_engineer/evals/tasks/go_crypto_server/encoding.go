package main

import (
	"errors"
	"strings"
)

const (
	customAlphabet = "3456789ABCDEFGHJKLMNPQRSTUVWXYZ$"
	customPad      = '~'
)

var customDecodeTable = func() [256]int8 {
	var t [256]int8
	for i := range t {
		t[i] = -1
	}
	for i := 0; i < len(customAlphabet); i++ {
		t[customAlphabet[i]] = int8(i)
	}
	return t
}()

func encodeBase32Custom(in []byte) string {
	var sb strings.Builder
	for i := 0; i < len(in); i += 5 {
		chunk := in[i:]
		if len(chunk) > 5 {
			chunk = chunk[:5]
		}
		var buf [5]byte
		copy(buf[:], chunk)
		bits := uint64(buf[0])<<32 | uint64(buf[1])<<24 | uint64(buf[2])<<16 | uint64(buf[3])<<8 | uint64(buf[4])
		out := [8]byte{
			customAlphabet[(bits>>35)&0x1F],
			customAlphabet[(bits>>30)&0x1F],
			customAlphabet[(bits>>25)&0x1F],
			customAlphabet[(bits>>20)&0x1F],
			customAlphabet[(bits>>15)&0x1F],
			customAlphabet[(bits>>10)&0x1F],
			customAlphabet[(bits>>5)&0x1F],
			customAlphabet[bits&0x1F],
		}
		emit := 8
		switch len(chunk) {
		case 1:
			emit = 2
		case 2:
			emit = 4
		case 3:
			emit = 5
		case 4:
			emit = 7
		}
		sb.Write(out[:emit])
		for j := emit; j < 8; j++ {
			sb.WriteByte(customPad)
		}
	}
	return sb.String()
}

func decodeBase32Custom(in string) ([]byte, error) {
	if len(in)%8 != 0 {
		return nil, errors.New("base32 input length not multiple of 8")
	}
	var out []byte
	for i := 0; i < len(in); i += 8 {
		group := in[i : i+8]
		var bits uint64
		valid := 0
		lastValue := int8(0)
		padding := false
		for j := 0; j < 8; j++ {
			c := group[j]
			if c == customPad {
				padding = true
				continue
			}
			if padding {
				return nil, errors.New("base32 data after padding")
			}
			v := customDecodeTable[c]
			if v < 0 {
				return nil, errors.New("invalid base32 character")
			}
			bits = (bits << 5) | uint64(v)
			lastValue = v
			valid++
		}
		if padding && i+8 != len(in) {
			return nil, errors.New("base32 padding before final group")
		}
		wantPadding := 0
		switch valid {
		case 2:
			wantPadding = 6
		case 4:
			wantPadding = 4
		case 5:
			wantPadding = 3
		case 7:
			wantPadding = 1
		case 8:
			wantPadding = 0
		default:
			return nil, errors.New("invalid base32 group length")
		}
		if padding && 8-valid != wantPadding {
			return nil, errors.New("invalid base32 padding length")
		}
		bits <<= uint(5 * (8 - valid))
		var raw [5]byte
		raw[0] = byte(bits >> 32)
		raw[1] = byte(bits >> 24)
		raw[2] = byte(bits >> 16)
		raw[3] = byte(bits >> 8)
		raw[4] = byte(bits)
		// The encoder leaves the unused low bits of a partial group clear. Reject
		// alternate bit strings that would decode to the same output bytes.
		unusedBits := uint(valid*5 - bytesForBase32Group(valid)*8)
		if unusedBits != 0 && uint8(lastValue)&(1<<unusedBits-1) != 0 {
			return nil, errors.New("non-zero base32 padding bits")
		}
		bytesOut := bytesForBase32Group(valid)
		out = append(out, raw[:bytesOut]...)
	}
	return out, nil
}

func bytesForBase32Group(chars int) int {
	switch chars {
	case 2:
		return 1
	case 4:
		return 2
	case 5:
		return 3
	case 7:
		return 4
	case 8:
		return 5
	default:
		return 0
	}
}

package main

import (
	"encoding/binary"
	"errors"
)

const (
	roundCount = 8
	blockSize  = 8
)

var sbox = [16]byte{
	0x9, 0x4, 0xA, 0xB, 0xD, 0x1, 0x8, 0x5,
	0x6, 0x2, 0x0, 0x3, 0xC, 0xE, 0xF, 0x7,
}

var roundConstants = [8]uint32{
	0xB7E15163,
	0x9E3779B9,
	0x243F6A88,
	0x85A308D3,
	0x13198A2E,
	0x03707344,
	0xA4093822,
	0x299F31D0,
}

const fMultiplier uint32 = 0x9E370001

func rotr32(x uint32, n uint) uint32 {
	return (x >> n) | (x << (32 - n))
}

func feistelF(r, k uint32) uint32 {
	idx := (r ^ k) & 0xF
	return uint32(sbox[idx])*fMultiplier ^ rotr32(r, 7)
}

func deriveRoundKeys(masterKey [16]byte) [roundCount]uint32 {
	k0 := binary.BigEndian.Uint32(masterKey[0:4])
	k1 := binary.BigEndian.Uint32(masterKey[4:8])
	k2 := binary.BigEndian.Uint32(masterKey[8:12])
	k3 := binary.BigEndian.Uint32(masterKey[12:16])
	var rk [roundCount]uint32
	state := [4]uint32{k0, k1, k2, k3}
	for i := 0; i < roundCount; i++ {
		mixed := state[0] + state[1] + state[2] + state[3] + roundConstants[i]
		rk[i] = mixed ^ rotr32(state[i&3], 3)
		state[0], state[1], state[2], state[3] = state[1], state[2], state[3], mixed
	}
	return rk
}

func encryptBlock(plain [blockSize]byte, rk [roundCount]uint32) [blockSize]byte {
	l := binary.BigEndian.Uint32(plain[0:4])
	r := binary.BigEndian.Uint32(plain[4:8])
	for i := 0; i < roundCount; i++ {
		l, r = r, l^feistelF(r, rk[i])
	}
	var out [blockSize]byte
	binary.BigEndian.PutUint32(out[0:4], l)
	binary.BigEndian.PutUint32(out[4:8], r)
	return out
}

func decryptBlock(cipher [blockSize]byte, rk [roundCount]uint32) [blockSize]byte {
	l := binary.BigEndian.Uint32(cipher[0:4])
	r := binary.BigEndian.Uint32(cipher[4:8])
	for i := roundCount - 1; i >= 0; i-- {
		l, r = r^feistelF(l, rk[i]), l
	}
	var out [blockSize]byte
	binary.BigEndian.PutUint32(out[0:4], l)
	binary.BigEndian.PutUint32(out[4:8], r)
	return out
}

func padPKCS7(in []byte) []byte {
	pad := blockSize - (len(in) % blockSize)
	out := make([]byte, len(in)+pad)
	copy(out, in)
	for i := len(in); i < len(out); i++ {
		out[i] = byte(pad)
	}
	return out
}

func unpadPKCS7(in []byte) ([]byte, error) {
	if len(in) == 0 || len(in)%blockSize != 0 {
		return nil, errors.New("bad padding length")
	}
	pad := int(in[len(in)-1])
	if pad == 0 || pad > blockSize {
		return nil, errors.New("bad padding byte")
	}
	for i := len(in) - pad; i < len(in); i++ {
		if int(in[i]) != pad {
			return nil, errors.New("bad padding tail")
		}
	}
	return in[:len(in)-pad], nil
}

func encryptECB(plain []byte, key [16]byte) []byte {
	rk := deriveRoundKeys(key)
	padded := padPKCS7(plain)
	out := make([]byte, len(padded))
	for i := 0; i < len(padded); i += blockSize {
		var blk [blockSize]byte
		copy(blk[:], padded[i:i+blockSize])
		enc := encryptBlock(blk, rk)
		copy(out[i:i+blockSize], enc[:])
	}
	return out
}

func decryptECB(cipher []byte, key [16]byte) ([]byte, error) {
	if len(cipher)%blockSize != 0 {
		return nil, errors.New("ciphertext length not multiple of block size")
	}
	rk := deriveRoundKeys(key)
	out := make([]byte, len(cipher))
	for i := 0; i < len(cipher); i += blockSize {
		var blk [blockSize]byte
		copy(blk[:], cipher[i:i+blockSize])
		dec := decryptBlock(blk, rk)
		copy(out[i:i+blockSize], dec[:])
	}
	return unpadPKCS7(out)
}

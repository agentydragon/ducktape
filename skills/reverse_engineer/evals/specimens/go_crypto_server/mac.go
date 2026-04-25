package main

import "encoding/binary"

const macTagSize = 8

var macIV = [blockSize]byte{
	0xA5, 0x5A, 0x33, 0xCC, 0x96, 0x69, 0xF0, 0x0F,
}

func macCompress(state, block [blockSize]byte, rk [roundCount]uint32) [blockSize]byte {
	var mixed [blockSize]byte
	for i := 0; i < blockSize; i++ {
		mixed[i] = state[i] ^ block[i]
	}
	return encryptBlock(mixed, rk)
}

func macSign(key [16]byte, msg []byte) [macTagSize]byte {
	rk := deriveRoundKeys(key)
	var preimage []byte
	preimage = append(preimage, key[:]...)
	var lenBytes [4]byte
	binary.BigEndian.PutUint32(lenBytes[:], uint32(len(key)))
	preimage = append(preimage, lenBytes[:]...)
	preimage = append(preimage, msg...)
	padded := padPKCS7(preimage)
	state := macIV
	for i := 0; i < len(padded); i += blockSize {
		var blk [blockSize]byte
		copy(blk[:], padded[i:i+blockSize])
		state = macCompress(state, blk, rk)
	}
	var tag [macTagSize]byte
	copy(tag[:], state[:])
	return tag
}

func macVerify(key [16]byte, msg []byte, tag [macTagSize]byte) bool {
	got := macSign(key, msg)
	var diff byte
	for i := 0; i < macTagSize; i++ {
		diff |= got[i] ^ tag[i]
	}
	return diff == 0
}

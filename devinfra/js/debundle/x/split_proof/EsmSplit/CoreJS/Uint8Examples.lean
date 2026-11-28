import EsmSplit.CoreJS.Number

open CoreJS

#eval do
  let cases : List (Float × UInt8) :=
    [
      (17, 17),
      (255.75, 255),
      (256, 0),
      (256.75, 0),
      (511.75, 255),
      (-0.75, 0),
      (-1.75, 255),
      (-255.75, 1),
      (-256.75, 0),
      (-257, 255),
      (4294967297.75, 1),
      (-4294967297.75, 255),
      (9007199254740991, 255),
      (9007199254740992, 0),
      (Float.ofBits 0x7fefffffffffffff, 0),
      (Float.ofBits 1, 0),
      (Float.ofBits 0x8000000000000000, 0),
      (Num.nan, 0),
      (Num.inf, 0),
      (-Num.inf, 0),
    ]
  for (number, expected) in cases do
    let actual := Num.toUint8 number
    unless actual == expected do
      throw
          (IO.userError
            s!"ToUint8 of Float bits {number.toBits}: expected {expected}, got {actual}")

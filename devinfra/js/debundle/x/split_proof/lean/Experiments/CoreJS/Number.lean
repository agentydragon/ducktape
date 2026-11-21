/-!
# CoreJS: numbers

A JavaScript number is an IEEE double: Lean's `Float`. `+ - * /` and the comparisons are the processor's.
What ECMAScript defines beyond them is computed here exactly, on the bits, with `Nat` arithmetic:
`Number::toString` (the shortest digits that round to the number), `StringToNumber`, `%`, `ToInt32` and `ToUint32`.
`**` is the C library's `pow` with ECMAScript's special cases.
-/

namespace CoreJS.Num

def nan : Float := 0.0 / 0.0
def inf : Float := 1.0 / 0.0

/-- The nearest double to `n / d`, ties to even, with the given sign. -/
def ofRatio (neg : Bool) (n d : Nat) : Float :=
  let scaled (k : Int) : Nat × Nat := if k ≥ 0 then (n, d <<< k.toNat) else (n <<< (-k).toNat, d)
  -- `⌊n / (d · 2^k)⌋` has 53 bits for `k = k0` or `k0 - 1`; a subnormal has fewer, at `k = -1074`
  let k0 : Int := (n.log2 : Int) - (d.log2 : Int) - 52
  let k1 := if (scaled k0).1 / (scaled k0).2 < 2 ^ 52 then k0 - 1 else k0
  let k := max k1 (-1074)
  let (a, b) := scaled k
  let q := a / b
  let r := a % b
  let q := if 2 * r > b || (2 * r == b && q % 2 == 1) then q + 1 else q
  let x := if n == 0 || d == 0 then 0.0 else Float.scaleB (Float.ofNat q) k
  if neg then -x else x

/-- A finite double as `(negative, m, e)`: its value is `±m · 2^e`. -/
def decompose (x : Float) : Bool × Nat × Int :=
  let bits := x.toBits.toNat
  let ex := (bits >>> 52) % 2048
  let frac := bits % 2 ^ 52
  (bits >>> 63 == 1, if ex == 0 then frac else frac + 2 ^ 52, ((if ex == 0 then 1 else ex : Nat) : Int) - 1075)

def isNegZero (x : Float) : Bool := x == 0.0 && (decompose x).1

/-- A double with its fraction dropped; 0 for a NaN or an infinity (`ToIntegerOrInfinity`, as `ToInt32` uses it). -/
def truncInt (x : Float) : Int :=
  if x.isNaN || x.isInf then 0 else
  let (neg, m, e) := decompose x
  let a : Nat := if e ≥ 0 then m <<< e.toNat else m >>> (-e).toNat
  if neg then -(a : Int) else a

def toUint32 (x : Float) : Nat := (Int.emod (truncInt x) (2 ^ 32)).toNat

def toInt32 (x : Float) : Int :=
  let u := toUint32 x
  if u ≥ 2 ^ 31 then (u : Int) - 2 ^ 32 else u

/-- `a % b`: the remainder has the sign of `a` (C's `fmod`). -/
def fmod (a b : Float) : Float :=
  if a.isNaN || b.isNaN || a.isInf || b == 0.0 then nan
  else if b.isInf || a == 0.0 then a
  else
    let (neg, ma, ea) := decompose a
    let (_, mb, eb) := decompose b
    let e := min ea eb
    let r := (ma <<< (ea - e).toNat) % (mb <<< (eb - e).toNat)
    if e ≥ 0 then ofRatio neg (r <<< e.toNat) 1 else ofRatio neg r (1 <<< (-e).toNat)

/-- `a ** b` (`Number::exponentiate`). -/
def pow (a b : Float) : Float :=
  if b.isNaN then nan
  else if b == 0.0 then 1.0
  else if b.isInf && (a == 1.0 || a == -1.0) then nan
  else Float.pow a b

/--
The shortest decimal digits `ds` and the exponent `n` with `x = 0.ds × 10^n`, for a finite `x > 0`: the fewest digits
that still round to `x`, and of those the closest to `x`.
-/
def shortest (x : Float) : List Char × Int :=
  let (_, m, e) := decompose x
  -- in units of `2^(e-2)`: `x` is `4m`, and what rounds to `x` lies between `lo` and `hi`
  let below := if m == 2 ^ 52 && e > -1074 then 1 else 2
  let e2 := e - 2
  let up (t : Nat) : Nat := if e2 ≥ 0 then t <<< e2.toNat else t
  let d : Nat := if e2 ≥ 0 then 1 else 1 <<< (-e2).toNat
  let v := up (4 * m)
  let lo := up (4 * m - below)
  let hi := up (4 * m + 2)
  let atLeast (p : Int) : Bool := if p ≥ 0 then v ≥ d * 10 ^ p.toNat else v * 10 ^ (-p).toNat ≥ d
  -- `n`: the least `p` with `x < 10^p`; the estimate from the bit lengths is within 2 of it
  let start : Int := Int.fdiv (((v.log2 : Int) - (d.log2 : Int)) * 30103) 100000 - 2
  let n : Int := start + (((List.range 8).find? fun (i : Nat) => !atLeast (start + (i : Int))).getD 0 : Nat)
  let pick (k : Nat) : Option (Nat × Nat) :=
    let a := (k - n).toNat
    let u := d * 10 ^ (n - k).toNat
    let (v, lo, hi) := (v * 10 ^ a, lo * 10 ^ a, hi * 10 ^ a)
    let inside (s : Nat) : Bool := if m % 2 == 0 then lo ≤ s * u && s * u ≤ hi else lo < s * u && s * u < hi
    let s := v / u
    let lower := v - s * u
    let upper := (s + 1) * u - v
    match inside s, inside (s + 1) with
    | true, true => some (k, if lower < upper || (lower == upper && s % 2 == 0) then s else s + 1)
    | true, false => some (k, s)
    | false, true => some (k, s + 1)
    | false, false => none
  let (k, s) := ((List.range 17).findSome? fun i => pick (i + 1)).getD (17, 0)
  let all := (toString s).toList
  ((all.reverse.dropWhile (· == '0')).reverse, n + all.length - k)

/-- `Number::toString` in radix 10. -/
def toString (x : Float) : String :=
  if x.isNaN then "NaN"
  else if x == 0.0 then "0"
  else if x.isInf then (if x < 0.0 then "-Infinity" else "Infinity")
  else
    let (ds, n) := shortest (if x < 0.0 then -x else x)
    let k : Int := ds.length
    let zeros (c : Int) : List Char := List.replicate c.toNat '0'
    let body : List Char :=
      if k ≤ n && n ≤ 21 then ds ++ zeros (n - k)
      else if 0 < n && n ≤ 21 then ds.take n.toNat ++ '.' :: ds.drop n.toNat
      else if -6 < n && n ≤ 0 then '0' :: '.' :: zeros (-n) ++ ds
      else
        let mantissa := if ds.length == 1 then ds else ds.take 1 ++ '.' :: ds.drop 1
        mantissa ++ 'e' :: (if n - 1 < 0 then '-' else '+') :: (Nat.repr (n - 1).natAbs).toList
    String.ofList (if x < 0.0 then '-' :: body else body)

/-- White space and line terminators that `StringToNumber` strips. -/
def isSpace (c : Char) : Bool :=
  let u := c.toNat
  (9 ≤ u && u ≤ 13) || u == 0x20 || u == 0xA0 || u == 0x1680 || (0x2000 ≤ u && u ≤ 0x200A) || u == 0x2028 ||
    u == 0x2029 || u == 0x202F || u == 0x205F || u == 0x3000 || u == 0xFEFF

def digitValue (c : Char) : Option Nat :=
  if '0' ≤ c && c ≤ '9' then some (c.toNat - '0'.toNat)
  else if 'a' ≤ c && c ≤ 'f' then some (c.toNat - 'a'.toNat + 10)
  else if 'A' ≤ c && c ≤ 'F' then some (c.toNat - 'A'.toNat + 10)
  else none

/-- The value of a nonempty run of digits of `base`. -/
def natOf (base : Nat) (cs : List Char) : Option Nat :=
  if cs.isEmpty then none
  else cs.foldlM (fun acc c => (digitValue c).bind fun v => if v < base then some (acc * base + v) else none) 0

/-- A decimal literal without a sign: digits, a fraction, an exponent. -/
def ofDecimal (neg : Bool) (cs : List Char) : Float :=
  let (whole, rest) := cs.span Char.isDigit
  let (fraction, rest) := match rest with
    | '.' :: more => more.span Char.isDigit
    | _ => ([], rest)
  let exponent : Option Int := match rest with
    | [] => some 0
    | c :: more =>
      if c != 'e' && c != 'E' then none else
      match more with
      | '-' :: ds => (natOf 10 ds).map fun v => -(v : Int)
      | '+' :: ds => (natOf 10 ds).map fun v => (v : Int)
      | ds => (natOf 10 ds).map fun v => (v : Int)
  match natOf 10 (whole ++ fraction), exponent with
  | some mantissa, some ex =>
    let ex := ex - fraction.length
    -- beyond these bounds the value is an infinity or a zero; the powers of ten are not computed
    if mantissa == 0 then ofRatio neg 0 1
    else if ex > 400 then (if neg then -inf else inf)
    else if ex < -400 - (whole.length + fraction.length : Nat) then ofRatio neg 0 1
    else if ex ≥ 0 then ofRatio neg (mantissa * 10 ^ ex.toNat) 1
    else ofRatio neg mantissa (10 ^ (-ex).toNat)
  | _, _ => nan

/-- `StringToNumber`. -/
def ofString (s : String) : Float :=
  let cs := ((s.toList.dropWhile isSpace).reverse.dropWhile isSpace).reverse
  let radix (base : Nat) (ds : List Char) : Float := ((natOf base ds).map fun v => ofRatio false v 1).getD nan
  match cs with
  | [] => 0.0
  | '0' :: 'x' :: ds | '0' :: 'X' :: ds => radix 16 ds
  | '0' :: 'o' :: ds | '0' :: 'O' :: ds => radix 8 ds
  | '0' :: 'b' :: ds | '0' :: 'B' :: ds => radix 2 ds
  | '-' :: ds => if ds == "Infinity".toList then -inf else ofDecimal true ds
  | '+' :: ds => if ds == "Infinity".toList then inf else ofDecimal false ds
  | ds => if ds == "Infinity".toList then inf else ofDecimal false ds

end CoreJS.Num

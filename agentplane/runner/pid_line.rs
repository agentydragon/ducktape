pub(crate) fn format_pid_line(pid: u32) -> ([u8; 12], usize) {
    let mut digits = [0u8; 12];
    let mut start = digits.len() - 1;
    digits[start] = b'\n';
    let mut remaining = pid;
    loop {
        start -= 1;
        digits[start] = b'0' + (remaining % 10) as u8;
        remaining /= 10;
        if remaining == 0 {
            break;
        }
    }
    (digits, start)
}

#[cfg(test)]
#[test]
fn pid_line_keeps_the_final_digit_before_the_newline() {
    for (pid, expected) in [
        (3, b"3\n".as_slice()),
        (123, b"123\n"),
        (u32::MAX, b"4294967295\n"),
    ] {
        let (buffer, start) = format_pid_line(pid);
        assert_eq!(&buffer[start..], expected);
    }
}

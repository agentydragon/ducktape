from inventree_utils.beautifier.assign_jellybean import parse_file_lines


def test_parse_file_lines():
    lines = ("# comment", "   # also comment", "foo bar baz", "a b c")
    assert list(parse_file_lines(lines)) == [["foo", "bar", "baz"], ["a", "b", "c"]]

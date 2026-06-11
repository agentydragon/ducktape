"""Tests for gmail_link formatting."""

from gmail_archiver.plan_display import gmail_link


def test_gmail_link_creates_clickable_hyperlink():
    """Test that gmail_link creates a Rich hyperlink markup."""
    message_id = "19b1c55967d81057"
    result = gmail_link(message_id)

    # Should contain Rich hyperlink markup
    expected_url = "https://mail.google.com/mail/#all/19b1c55967d81057"
    expected = f"[link={expected_url}]{message_id}[/link]"

    assert result == expected
    # Verify it contains both the URL and the message ID
    assert expected_url in result
    assert message_id in result
    assert "[link=" in result
    assert "[/link]" in result

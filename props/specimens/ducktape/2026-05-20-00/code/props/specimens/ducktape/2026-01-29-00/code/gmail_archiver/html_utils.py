"""HTML parsing utilities."""

from bs4 import BeautifulSoup


def html_to_text(html: str) -> str:
    """Extract clean text from HTML body.

    Removes script/style tags and normalizes whitespace.
    """
    soup = BeautifulSoup(html, "html.parser")

    # Remove script and style elements
    for script in soup(["script", "style"]):
        script.decompose()

    # Get text
    text = soup.get_text()

    # Clean up whitespace
    lines = (line.strip() for line in text.splitlines())
    chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
    return " ".join(chunk for chunk in chunks if chunk)

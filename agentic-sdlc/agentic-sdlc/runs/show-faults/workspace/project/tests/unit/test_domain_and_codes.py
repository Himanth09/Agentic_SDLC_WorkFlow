# Unit tests for the pure domain functions: URL and alias validation, base62 codes,
# user-agent classification, and the privacy helpers (referrer and IP reduction).
import pytest

from shortener.analytics import anonymize_ip
from shortener.codes import ALPHABET, CodeGenerator, decode_base62, encode_base62
from shortener.domain import classify_user_agent, referrer_host, validate_alias, validate_url
from shortener.errors import InvalidAlias, InvalidUrl


# Plain, with path/query/fragment, mixed case with a port, and a public IPv4 literal.
@pytest.mark.parametrize("url", [
    "https://example.com", "http://example.com/a/b?q=1#frag", "HTTPS://Example.COM:8443/x",
    "https://93.184.216.34/page"])
def test_valid_urls_are_accepted_and_normalised(url):
    """Covers: AC-VALIDATE"""
    out = validate_url(url, 2048)
    # Scheme is lower-cased and surrounding whitespace removed.
    assert out.startswith(("http://", "https://"))
    assert out == out.strip()


def test_url_normalisation_lowercases_scheme_and_host():
    """Covers: AC-VALIDATE"""
    # The path keeps its case: paths are case-sensitive on most servers.
    assert validate_url("HTTPS://Example.COM/Path", 2048) == "https://example.com/Path"


# Dangerous schemes, embedded credentials, local/private/link-local/cloud-metadata
# addresses, IPv6 loopback, and malformed or non-string input.
@pytest.mark.parametrize("url", [
    "javascript:alert(1)", "data:text/html,hi", "ftp://example.com/file", "file:///etc/passwd",
    "https://user:pass@example.com", "http://localhost:8080/admin", "http://127.0.0.1/",
    "http://10.0.0.5/", "http://192.168.1.1/", "http://169.254.169.254/latest/meta-data",
    "http://[::1]/", "https://", "", "   ", None, 42, "example.com"])
def test_unsafe_or_malformed_urls_are_rejected(url):
    """Covers: AC-VALIDATE"""
    with pytest.raises(InvalidUrl):
        validate_url(url, 2048)


def test_overlong_url_rejected():
    """Covers: AC-VALIDATE"""
    with pytest.raises(InvalidUrl):
        validate_url("https://example.com/" + "a" * 3000, 2048)


# Hyphen, underscore with mixed case, and the 4-character minimum.
@pytest.mark.parametrize("alias", ["promo-2026", "Black_Friday", "abcd"])
def test_valid_alias(alias):
    """Covers: AC-ALIAS"""
    assert validate_alias(alias) == alias


# Too short, too long, space, non-ASCII, reserved words (any case), and a non-string.
@pytest.mark.parametrize("alias", ["abc", "x" * 33, "has space", "emoji😀", "api", "HEALTHZ", 7])
def test_invalid_or_reserved_alias(alias):
    """Covers: AC-ALIAS"""
    with pytest.raises(InvalidAlias):
        validate_alias(alias)


def test_base62_roundtrip():
    # Edge values: zero, single digit, digit rollover, two-digit maximum, a 64-bit value.
    for n in (0, 1, 61, 62, 3843, 2**63 - 1):
        assert decode_base62(encode_base62(n)) == n
    # Left-padding to a fixed width.
    assert encode_base62(5, width=4) == "0005"
    # Characters outside the alphabet are rejected.
    with pytest.raises(ValueError):
        decode_base62("abc!")


def test_code_generator_length_and_alphabet():
    """Covers: AC-CONCURRENCY"""
    gen = CodeGenerator(7)
    # 2000 random codes from a space of 62^7 should never collide.
    codes = {gen.next() for _ in range(2000)}
    assert len(codes) == 2000
    # Fixed length and only alphabet characters.
    assert all(len(c) == 7 and set(c) <= set(ALPHABET) for c in codes)


# Missing header, a crawler, a command-line client, a phone and a desktop browser.
@pytest.mark.parametrize("ua,expected", [
    (None, "unknown"), ("Googlebot/2.1", "bot"), ("curl/8.0", "bot"),
    ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0)", "mobile"),
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "desktop")])
def test_user_agent_classification(ua, expected):
    """Covers: AC-ANALYTICS"""
    assert classify_user_agent(ua) == expected


def test_referrer_is_reduced_to_host():
    """Covers: AC-PRIVACY"""
    # The query string (which here contains an email address) is discarded; the host is lower-cased.
    assert referrer_host("https://News.example.org/a?email=x@y.z") == "news.example.org"
    assert referrer_host(None) is None


def test_ip_anonymisation_modes():
    """Covers: AC-PRIVACY"""
    ts = 1_700_000_000.0
    hashed = anonymize_ip("198.51.100.4", "hash", "salt", ts)
    # A short hash that doesn't contain the IP.
    assert hashed and "198.51.100.4" not in hashed and len(hashed) == 16
    assert hashed == anonymize_ip("198.51.100.4", "hash", "salt", ts + 60)      # stable within day
    assert hashed != anonymize_ip("198.51.100.4", "hash", "salt", ts + 86_400)  # rotates daily
    # "drop" mode keeps nothing.
    assert anonymize_ip("198.51.100.4", "drop", "salt", ts) is None

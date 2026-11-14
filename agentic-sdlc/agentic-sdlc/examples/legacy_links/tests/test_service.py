import pytest

from links.service import LinkService, NotFound
from links.store import LinkStore


@pytest.fixture
def service():
    return LinkService(LinkStore())


def test_shorten_and_resolve(service):
    link = service.shorten("https://example.com")
    assert service.resolve(link.code) == "https://example.com"


def test_clicks_are_counted(service):
    link = service.shorten("https://example.com")
    service.resolve(link.code)
    service.resolve(link.code)
    assert service.info(link.code).clicks == 2


def test_rejects_non_http(service):
    with pytest.raises(ValueError):
        service.shorten("ftp://example.com")


def test_unknown_code(service):
    with pytest.raises(NotFound):
        service.resolve("missing")


def test_custom_alias(service):
    link = service.shorten("https://example.com", alias="promo")
    assert link.code == "promo"
    assert service.resolve("promo") == "https://example.com"

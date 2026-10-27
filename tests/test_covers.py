from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app import covers
from app.config import Settings, get_settings
from app.scraper.client import FetchError, MetacriticClient

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 100
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 100
URL = "https://www.metacritic.com/a/img/catalog/provider/6/12/6-1-1289170-52.jpg"


class FakeClient:
    def __init__(self, data=JPEG):
        self.data, self.calls = data, 0

    def get_bytes(self, url, max_bytes):
        self.calls += 1
        return self.data


def test_image_type_is_taken_from_the_content():
    assert covers.image_ext(JPEG) == "jpg"
    assert covers.image_ext(PNG) == "png"
    assert covers.image_ext(WEBP) == "webp"
    assert covers.image_ext(b"GIF89a....") == "gif"
    assert covers.image_ext(b"<!doctype html><html>") is None


def test_cover_is_downloaded_once_and_named_by_url(tmp_path):
    client = FakeClient()
    name = covers.ensure_cover(client, tmp_path, URL)
    assert covers.FILE_RE.match(name) and name.endswith(".jpg")
    assert (tmp_path / name).read_bytes() == JPEG
    assert covers.ensure_cover(client, tmp_path, URL) == name
    assert client.calls == 1  # already stored: no second request
    assert covers.ensure_cover(client, tmp_path, None) is None
    assert not list(tmp_path.glob(".tmp-*"))


def test_non_images_and_huge_files_are_rejected(tmp_path):
    with pytest.raises(covers.CoverError):
        covers.ensure_cover(FakeClient(b"<html>Access denied</html>"), tmp_path, URL)
    with pytest.raises(covers.CoverError):
        covers.save(tmp_path, URL, JPEG + b"\x00" * covers.MAX_BYTES)
    assert not any(tmp_path.iterdir())


def test_client_downloads_bytes_with_retries_and_size_limit():
    statuses = iter([503, 200, 200])

    def handler(request):
        return httpx.Response(next(statuses), content=JPEG)

    client = MetacriticClient(
        Settings(http_backoff_base_seconds=0), SimpleNamespace(wait=lambda: None),
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert client.get_bytes(URL, max_bytes=1000) == JPEG
    with pytest.raises(FetchError):
        client.get_bytes(URL, max_bytes=10)


@pytest.fixture
def web(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.web.main import app

    monkeypatch.setenv("COVERS_DIR", str(tmp_path))
    get_settings.cache_clear()
    yield TestClient(app), tmp_path
    get_settings.cache_clear()


def test_stored_cover_is_served_and_cached_forever(web):
    client, covers_dir = web
    name = covers.save(covers_dir, URL, JPEG)
    r = client.get(f"/covers/{name}")
    assert r.status_code == 200 and r.content == JPEG
    assert r.headers["content-type"] == "image/jpeg"
    assert "immutable" in r.headers["cache-control"]


@pytest.mark.parametrize("name", ["missing" + "0" * 33 + ".jpg", "../../etc/passwd", "a.jpg", "%2e%2e%2fsecret"])
def test_unknown_or_unsafe_cover_names_are_404(web, name):
    client, _ = web
    assert client.get(f"/covers/{name}").status_code == 404


def test_cards_use_the_local_copy_when_there_is_one():
    from app.web.main import templates

    g = SimpleNamespace(id=1, slug="abyss-ring", title="Abyss Ring", release_date=None, platforms=[],
                        best_metascore=None, best_user_score=None, cover_url=URL, cover_file=None)
    tpl = templates.get_template("_game_card.html")
    assert f'src="{URL}"' in tpl.render(g=g)
    g.cover_file = covers.url_key(URL) + ".jpg"
    html = tpl.render(g=g)
    assert f'src="/covers/{g.cover_file}"' in html and URL not in html
    assert "cover-tbd" in html  # the striped TBD placeholder is still underneath

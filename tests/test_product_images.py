# -*- coding: utf-8 -*-
"""pipeline.product_images のテスト。実ネットワーク/実ffprobeは一切使わず、
fetcher/proberをすべてスタブ注入してロジックのみを検証する。"""
import os
import urllib.error
from urllib.request import Request

import pytest

from pipeline import product_images


# ---------------------------------------------------------------------------
# (e) _is_safe_url: 不正URL拒否
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/product",
        "http://127.0.0.1/product",
        "http://127.0.0.5/product",
        "http://10.0.0.5/product",
        "http://192.168.1.5/product",
        "http://172.16.0.5/product",
        "http://172.31.255.255/product",
        "http://[::1]/product",
        "ftp://example.com/product",
        "example.com/product",  # scheme無し
        "",
        None,
    ],
)
def test_is_safe_url_rejects_private_or_non_http(url):
    assert product_images._is_safe_url(url) is False


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/product",
        "http://shop.example.co.jp/item/123",
        "https://172.32.0.1/product",  # 172.32は31を超えるのでprivate range外
    ],
)
def test_is_safe_url_accepts_normal_public_urls(url):
    assert product_images._is_safe_url(url) is True


# ---------------------------------------------------------------------------
# (a) extract_page_info: og:image優先・相対URL解決・svg除外
# ---------------------------------------------------------------------------

def test_extract_page_info_prioritizes_og_image_and_resolves_relative_urls():
    html = """
    <html><head>
      <title>フォールバックタイトル</title>
      <meta property="og:title" content="うるおい美容液A">
      <meta property="og:image" content="/img/og-main.jpg">
      <meta name="twitter:image" content="https://cdn.example.com/twitter.jpg">
    </head><body>
      <img src="favicon.ico">
      <img src="/img/logo.svg">
      <img data-src="/img/product-1.jpg">
      <img src="data:image/png;base64,AAAA">
      <img src="/img/product-2.png">
    </body></html>
    """
    info = product_images.extract_page_info(html, "https://shop.example.com/lp/a")

    assert info["title"] == "うるおい美容液A"
    # og:image / twitter:image が最優先、続いて<img>が文書順
    assert info["image_urls"][0] == "https://shop.example.com/img/og-main.jpg"
    assert info["image_urls"][1] == "https://cdn.example.com/twitter.jpg"
    assert "https://shop.example.com/img/product-1.jpg" in info["image_urls"]
    assert "https://shop.example.com/img/product-2.png" in info["image_urls"]
    # 除外対象
    joined = " ".join(info["image_urls"])
    assert "favicon" not in joined
    assert ".svg" not in joined
    assert "data:image" not in joined


def test_extract_page_info_falls_back_to_title_tag_when_no_og_title():
    html = "<html><head><title>タイトルのみ</title></head><body><img src=\"/a.jpg\"></body></html>"
    info = product_images.extract_page_info(html, "https://example.com/lp")
    assert info["title"] == "タイトルのみ"


def test_extract_page_info_picks_highest_resolution_from_srcset():
    html = (
        '<html><body><img srcset="/img/small.jpg 400w, /img/large.jpg 1200w, /img/mid.jpg 800w" '
        'src="/img/fallback.jpg"></body></html>'
    )
    info = product_images.extract_page_info(html, "https://example.com/lp")
    assert info["image_urls"] == ["https://example.com/img/large.jpg"]


def test_extract_page_info_dedupes_by_resolved_url():
    html = (
        '<html><head><meta property="og:image" content="https://example.com/img/a.jpg"></head>'
        '<body><img src="/img/a.jpg"></body></html>'
    )
    info = product_images.extract_page_info(html, "https://example.com/lp")
    assert info["image_urls"] == ["https://example.com/img/a.jpg"]


# ---------------------------------------------------------------------------
# collect_product_images 用の共通スタブ
# ---------------------------------------------------------------------------

_BIG_DIMS = (800, 1000)
_SMALL_DIMS = (200, 250)  # 短辺400px未満


def _make_prober(dims_by_marker):
    """ファイル内容の先頭マーカー文字列から寸法を返すproberスタブ。"""

    def _prober(path):
        with open(path, "rb") as f:
            data = f.read()
        for marker, dims in dims_by_marker.items():
            if data.startswith(marker):
                return dims
        return None

    return _prober


def _make_fetcher(responses):
    def _fetcher(url):
        if url not in responses:
            raise RuntimeError("unexpected url: {}".format(url))
        return responses[url]

    return _fetcher


_LP_HTML = (
    '<html><head><meta property="og:title" content="商品X">'
    '<meta property="og:image" content="/img/og.jpg"></head>'
    '<body>'
    '<img src="/img/small.jpg">'
    '<img src="/img/dup.jpg">'
    '<img src="/img/dup.jpg">'
    '</body></html>'
).encode("utf-8")


# ---------------------------------------------------------------------------
# (b) 解像度フィルタ・重複排除・上限
# ---------------------------------------------------------------------------

def test_collect_product_images_filters_small_and_dedupes_and_caps(tmp_path):
    responses = {
        "https://shop.example.com/lp": _LP_HTML,
        "https://shop.example.com/img/og.jpg": b"BIG:og",
        "https://shop.example.com/img/small.jpg": b"SMALL:small",
        "https://shop.example.com/img/dup.jpg": b"BIG:dup",
    }
    fetcher = _make_fetcher(responses)
    prober = _make_prober({b"BIG:": _BIG_DIMS, b"SMALL:": _SMALL_DIMS})

    result = product_images.collect_product_images(
        "https://shop.example.com/lp", str(tmp_path), cfg={"max_images": 6}, fetcher=fetcher, prober=prober
    )

    assert result["name"] == "商品X"
    # og.jpg(big) 採用、small.jpg(短辺<400)除外、dup.jpgは同一内容が2回出現するがsha256重複排除で1枚のみ
    assert len(result["images"]) == 2
    for img in result["images"]:
        assert img["source"] == "lp"
        assert min(img["width"], img["height"]) >= 400
        assert os.path.exists(img["path"])


def test_collect_product_images_caps_at_max_images(tmp_path):
    n = 10
    html_imgs = "".join('<img src="/img/p{}.jpg">'.format(i) for i in range(n))
    html = "<html><body>{}</body></html>".format(html_imgs).encode("utf-8")
    responses = {"https://shop.example.com/lp": html}
    for i in range(n):
        responses["https://shop.example.com/img/p{}.jpg".format(i)] = "BIG:{}".format(i).encode("utf-8")

    fetcher = _make_fetcher(responses)
    prober = _make_prober({b"BIG:": _BIG_DIMS})

    result = product_images.collect_product_images(
        "https://shop.example.com/lp", str(tmp_path), cfg={"max_images": 6}, fetcher=fetcher, prober=prober
    )
    assert len(result["images"]) == 6


# ---------------------------------------------------------------------------
# (c) local_dir優先
# ---------------------------------------------------------------------------

def test_collect_product_images_prefers_local_dir_first(tmp_path):
    local_dir = tmp_path / "local"
    local_dir.mkdir()
    (local_dir / "a.jpg").write_bytes(b"BIG:local_a")
    (local_dir / "b.png").write_bytes(b"BIG:local_b")

    dest_dir = tmp_path / "dest"

    responses = {
        "https://shop.example.com/lp": _LP_HTML,
        "https://shop.example.com/img/og.jpg": b"BIG:og",
        "https://shop.example.com/img/small.jpg": b"SMALL:small",
        "https://shop.example.com/img/dup.jpg": b"BIG:dup",
    }
    fetcher = _make_fetcher(responses)
    prober = _make_prober({b"BIG:": _BIG_DIMS, b"SMALL:": _SMALL_DIMS})

    result = product_images.collect_product_images(
        "https://shop.example.com/lp",
        str(dest_dir),
        cfg={"max_images": 6},
        local_dir=str(local_dir),
        fetcher=fetcher,
        prober=prober,
    )

    sources = [img["source"] for img in result["images"]]
    # local優先で先頭2件がlocal、続いてlp
    assert sources[:2] == ["local", "local"]
    assert "lp" in sources[2:]


def test_collect_product_images_local_dir_only_when_it_fills_max_images(tmp_path):
    local_dir = tmp_path / "local"
    local_dir.mkdir()
    (local_dir / "a.jpg").write_bytes(b"BIG:1")
    (local_dir / "b.jpg").write_bytes(b"BIG:2")

    def _fetcher_should_not_be_called(url):
        raise AssertionError("LP fetch should be skipped when local_dir already fills max_images")

    prober = _make_prober({b"BIG:": _BIG_DIMS})
    result = product_images.collect_product_images(
        "https://shop.example.com/lp",
        str(tmp_path / "dest"),
        cfg={"max_images": 2},
        local_dir=str(local_dir),
        fetcher=_fetcher_should_not_be_called,
        prober=prober,
    )
    assert len(result["images"]) == 2
    assert all(img["source"] == "local" for img in result["images"])


# ---------------------------------------------------------------------------
# (e) 不正URL拒否（collect_product_images経由）
# ---------------------------------------------------------------------------

def test_collect_product_images_rejects_unsafe_lp_url(tmp_path):
    def _fetcher_should_not_be_called(url):
        raise AssertionError("private URLはfetchされてはならない")

    result = product_images.collect_product_images(
        "http://127.0.0.1/product", str(tmp_path), cfg={}, fetcher=_fetcher_should_not_be_called
    )
    assert result["images"] == []
    assert any("不正" in w for w in result["warnings"])


def test_collect_product_images_never_raises_and_returns_dict_on_fetch_error(tmp_path):
    def _raising_fetcher(url):
        raise RuntimeError("network down")

    result = product_images.collect_product_images(
        "https://shop.example.com/lp", str(tmp_path), cfg={}, fetcher=_raising_fetcher
    )
    assert result["images"] == []
    assert result["warnings"]


# ---------------------------------------------------------------------------
# (d) assign_images_to_shots: フック/CTA/中間/枯渇
# ---------------------------------------------------------------------------

def _shots(n):
    return [{"id": "s{}".format(i + 1), "caption_jp": "c{}".format(i + 1)} for i in range(n)]


def test_assign_images_to_shots_hook_and_cta_and_even_middle_distribution():
    shots = _shots(5)
    images = ["p0.jpg", "p1.jpg", "p2.jpg", "p3.jpg", "p4.jpg"]
    result = product_images.assign_images_to_shots(shots, images)

    assert result[0]["image_path"] == "p0.jpg"  # フック
    assert result[4]["image_path"] == "p1.jpg"  # CTA(2枚以上あるので[1])
    assert result[1]["image_path"] == "p2.jpg"
    assert result[2]["image_path"] == "p3.jpg"
    assert result[3]["image_path"] == "p4.jpg"
    # 非破壊: 元のshotsは変更されない
    assert "image_path" not in shots[0]


def test_assign_images_to_shots_exhausted_middle_shots_have_no_key():
    shots = _shots(6)
    images = ["p0.jpg", "p1.jpg", "p2.jpg"]  # hook+cta+1枚のみ中間へ
    result = product_images.assign_images_to_shots(shots, images)

    assert result[0]["image_path"] == "p0.jpg"
    assert result[5]["image_path"] == "p1.jpg"
    assigned_middle = [i for i in range(1, 5) if "image_path" in result[i]]
    assert len(assigned_middle) == 1
    assert result[assigned_middle[0]]["image_path"] == "p2.jpg"
    for i in range(1, 5):
        if i not in assigned_middle:
            assert "image_path" not in result[i]


def test_assign_images_to_shots_single_image_reused_for_hook_and_cta():
    shots = _shots(3)
    images = ["only.jpg"]
    result = product_images.assign_images_to_shots(shots, images)
    assert result[0]["image_path"] == "only.jpg"
    assert result[2]["image_path"] == "only.jpg"
    assert "image_path" not in result[1]


def test_assign_images_to_shots_no_images_returns_shots_without_image_path():
    shots = _shots(3)
    result = product_images.assign_images_to_shots(shots, [])
    assert result == shots
    for s in result:
        assert "image_path" not in s


def test_assign_images_to_shots_single_shot_gets_first_image():
    shots = _shots(1)
    result = product_images.assign_images_to_shots(shots, ["p0.jpg", "p1.jpg"])
    assert result[0]["image_path"] == "p0.jpg"


# ---------------------------------------------------------------------------
# 回帰(1): _is_safe_resolved_host — DNS rebinding対策(実解決ベースの私有IP判定)
# ---------------------------------------------------------------------------

def test_is_safe_resolved_host_rejects_private_resolution(monkeypatch):
    """一見安全なホスト名でも、DNS解決結果が私有IPなら拒否する（DNS rebinding対策）。"""
    monkeypatch.setattr(
        product_images.socket, "getaddrinfo", lambda host, port: [(2, 1, 6, "", ("169.254.169.254", 0))]
    )
    assert product_images._is_safe_resolved_host("evil.example.com") is False


def test_is_safe_resolved_host_accepts_public_resolution(monkeypatch):
    monkeypatch.setattr(
        product_images.socket, "getaddrinfo", lambda host, port: [(2, 1, 6, "", ("93.184.216.34", 0))]
    )
    assert product_images._is_safe_resolved_host("example.com") is True


def test_is_safe_resolved_host_rejects_on_resolution_failure(monkeypatch):
    def _raise(host, port):
        raise OSError("no dns")

    monkeypatch.setattr(product_images.socket, "getaddrinfo", _raise)
    assert product_images._is_safe_resolved_host("nonexistent.invalid") is False


def test_is_safe_resolved_host_rejects_empty_hostname():
    assert product_images._is_safe_resolved_host("") is False
    assert product_images._is_safe_resolved_host(None) is False


# ---------------------------------------------------------------------------
# 回帰(2): _SafeRedirectHandler — リダイレクト追従によるSSRF対策の抜けを塞ぐ
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "bad_url",
    [
        "http://127.0.0.1/steal",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/x",
        "http://10.0.0.5/internal",
    ],
)
def test_safe_redirect_handler_rejects_private_ip_redirect_targets(bad_url):
    handler = product_images._SafeRedirectHandler()
    req = Request("https://shop.example.com/lp")
    with pytest.raises(urllib.error.URLError):
        handler.redirect_request(req, None, 302, "Found", {}, bad_url)


def test_safe_redirect_handler_rejects_dns_rebinding_hostname(monkeypatch):
    """URL文字列上は私有IPリテラルでない(=_is_safe_urlは通る)ホスト名でも、
    実際のDNS解決結果が私有IPなら拒否する。"""

    def _fake_getaddrinfo(host, port):
        assert host == "evil.example.com"
        return [(2, 1, 6, "", ("169.254.169.254", 0))]

    monkeypatch.setattr(product_images.socket, "getaddrinfo", _fake_getaddrinfo)
    handler = product_images._SafeRedirectHandler()
    req = Request("https://shop.example.com/lp")
    with pytest.raises(urllib.error.URLError):
        handler.redirect_request(req, None, 302, "Found", {}, "https://evil.example.com/x")


def test_safe_redirect_handler_allows_safe_redirect_target(monkeypatch):
    monkeypatch.setattr(
        product_images.socket, "getaddrinfo", lambda host, port: [(2, 1, 6, "", ("93.184.216.34", 0))]
    )
    handler = product_images._SafeRedirectHandler()
    req = Request("https://shop.example.com/lp", method="GET")
    new_req = handler.redirect_request(req, None, 302, "Found", {}, "https://cdn.example.com/img.jpg")
    assert new_req.full_url == "https://cdn.example.com/img.jpg"


def test_safe_redirect_handler_caps_max_redirections():
    assert product_images._SafeRedirectHandler.max_redirections == 5


# ---------------------------------------------------------------------------
# 回帰(3): ダウンロードサイズ無制限対策 — LP HTML/画像のサイズ上限
# ---------------------------------------------------------------------------

def test_collect_product_images_discards_oversized_lp_html_with_warning(tmp_path):
    big_html = b"<html>" + b"x" * 100 + b"</html>"

    def _fetcher(url):
        return big_html

    result = product_images.collect_product_images(
        "https://shop.example.com/lp", str(tmp_path), cfg={"max_html_bytes": 5}, fetcher=_fetcher
    )
    assert result["images"] == []
    assert any("商品LP" in w and "上限" in w for w in result["warnings"])


def test_collect_product_images_discards_oversized_image_with_warning(tmp_path):
    html = (
        '<html><head><meta property="og:image" content="/img/big.jpg"></head><body></body></html>'
    ).encode("utf-8")
    responses = {
        "https://shop.example.com/lp": html,
        "https://shop.example.com/img/big.jpg": b"BIG:" + b"x" * 50,
    }
    fetcher = _make_fetcher(responses)

    result = product_images.collect_product_images(
        "https://shop.example.com/lp", str(tmp_path), cfg={"max_download_bytes": 10}, fetcher=fetcher
    )
    assert result["images"] == []
    assert any("画像サイズが上限" in w for w in result["warnings"])


def test_collect_product_images_default_size_limits_do_not_discard_normal_responses(tmp_path):
    """既定の上限(10MB画像/2MB HTML)は通常サイズのレスポンスを誤って破棄しないこと。"""
    responses = {
        "https://shop.example.com/lp": _LP_HTML,
        "https://shop.example.com/img/og.jpg": b"BIG:og",
        "https://shop.example.com/img/small.jpg": b"SMALL:small",
        "https://shop.example.com/img/dup.jpg": b"BIG:dup",
    }
    fetcher = _make_fetcher(responses)
    prober = _make_prober({b"BIG:": _BIG_DIMS, b"SMALL:": _SMALL_DIMS})

    result = product_images.collect_product_images(
        "https://shop.example.com/lp", str(tmp_path), cfg={}, fetcher=fetcher, prober=prober
    )
    assert len(result["images"]) == 2
    assert not any("上限" in w for w in result["warnings"])


# ---------------------------------------------------------------------------
# 回帰(4): _build_default_fetcher — cfgのtimeout_sec/max_download_bytesの配線
# ---------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, data):
        self._data = data

    def read(self, n=-1):
        if n is None or n < 0:
            return self._data
        return self._data[:n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_build_default_fetcher_wires_cfg_timeout_and_enforces_max_bytes(monkeypatch):
    captured = {}

    class _FakeOpener:
        def open(self, req, timeout=None):
            captured["timeout"] = timeout
            return _FakeResp(b"0123456789")  # 10 bytes

    monkeypatch.setattr(product_images, "_is_safe_resolved_host", lambda host: True)
    monkeypatch.setattr(product_images, "build_opener", lambda handler_cls: _FakeOpener())

    fetch_small_limit = product_images._build_default_fetcher({"timeout_sec": 42, "max_download_bytes": 5})
    with pytest.raises(product_images.DownloadTooLargeError):
        fetch_small_limit("https://example.com/big.jpg")
    assert captured["timeout"] == 42

    fetch_ok = product_images._build_default_fetcher({"timeout_sec": 7, "max_download_bytes": 100})
    data = fetch_ok("https://example.com/small.jpg")
    assert data == b"0123456789"
    assert captured["timeout"] == 7


def test_build_default_fetcher_rejects_before_network_when_dns_resolves_to_private_ip(monkeypatch):
    """DNS解決結果が私有IPの場合、opener.open自体を呼ばずに拒否すること。"""

    def _opener_should_not_be_called(handler_cls):
        raise AssertionError("DNS解決が私有IPのときはopenerを構築してはいけない")

    monkeypatch.setattr(product_images, "_is_safe_resolved_host", lambda host: False)
    monkeypatch.setattr(product_images, "build_opener", _opener_should_not_be_called)

    fetch = product_images._build_default_fetcher({})
    with pytest.raises(urllib.error.URLError):
        fetch("https://evil.example.com/x")

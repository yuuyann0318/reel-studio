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
# 回帰: 制御文字/空白入りURLはfetch段まで通さず extract_page_info で捨てる
# 実機発生: LP HTMLに Liquid テンプレのエラー文字列がそのまま埋まっており、
# 生成された img src が "https://ct.pinterest.com/v3/?pd[em]=Liquid error: internal&noscript=1"
# のような半角スペースを含むURLになる → 従来は _fetch 直前で ValueError("URL can't contain
# control characters") が上がり、"画像の取得に失敗しました" ノイズとして project.json の
# warnings に記録され、UIには「画像が読み込めない」ように見えていた（ユーザー報告）。
# ---------------------------------------------------------------------------


def test_has_control_or_ws_chars_detects_space_tab_newline_and_c0():
    for bad in [
        "https://example.com/path with space",
        "https://example.com/path\ttab",
        "https://example.com/path\nnewline",
        "https://example.com/path\r",
        "https://example.com/\x01",
        "https://example.com/\x7f",
    ]:
        assert product_images._has_control_or_ws_chars(bad) is True, bad
    for good in [
        "https://example.com/img.png",
        "https://example.com/path?a=b&c=d",
        "https://example.com/path%20encoded",
        "https://example.com/日本語パス.png",  # 非ASCIIは制御文字ではない
    ]:
        assert product_images._has_control_or_ws_chars(good) is False, good


def test_is_safe_url_rejects_url_with_control_chars_or_space():
    liquid_url = (
        "https://ct.pinterest.com/v3/?event=init&tid=2613103650650"
        "&pd[em]=Liquid error: internal&noscript=1"
    )
    assert product_images._is_safe_url(liquid_url) is False
    assert product_images._is_safe_url("https://example.com/path\nx") is False
    assert product_images._is_safe_url("https://example.com/normal.png") is True


def test_extract_page_info_drops_urls_with_control_chars():
    html = (
        '<html><head><title>t</title></head><body>'
        '<img src="https://ok.example.com/good.png">'
        # Liquid テンプレのレンダリング事故で埋まったURL（半角スペース入り）
        '<img src="https://ct.pinterest.com/v3/?pd[em]=Liquid error: internal&noscript=1">'
        '<img src="https://another.example.com/path with space.jpg">'
        '</body></html>'
    )
    info = product_images.extract_page_info(html, "https://shop.example.com/lp/a")
    assert info["image_urls"] == ["https://ok.example.com/good.png"]


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


# ---------------------------------------------------------------------------
# (h) classify_product_images: vision分類 + 縮退 + manifest 保存
# ---------------------------------------------------------------------------

def test_classify_product_images_categorizes_and_filters_low_or_logo():
    """visionが4カテゴリを返した場合、product_solo/in_use は採用、logo_banner/unrelated と
    sharpness=low は不採用（adopted=False + reason 付き）。"""
    def _fake_vision(prompt, paths, timeout_sec=600):
        return {
            "ok": True,
            "data": [
                {"index": 1, "category": "product_solo", "sharpness": "high",
                 "dominant_colors": ["#ff0000", "#ffffff"]},
                {"index": 2, "category": "product_in_use", "sharpness": "high",
                 "dominant_colors": ["#123456"]},
                {"index": 3, "category": "logo_banner", "sharpness": "high",
                 "dominant_colors": []},
                {"index": 4, "category": "unrelated", "sharpness": "high",
                 "dominant_colors": []},
                {"index": 5, "category": "product_solo", "sharpness": "low",
                 "dominant_colors": []},
            ],
        }
    paths = ["/tmp/a.jpg", "/tmp/b.jpg", "/tmp/c.jpg", "/tmp/d.jpg", "/tmp/e.jpg"]
    entries = product_images.classify_product_images(paths, vision_call=_fake_vision)
    assert [e["adopted"] for e in entries] == [True, True, False, False, False]
    assert entries[0]["category"] == "product_solo"
    assert entries[0]["dominant_colors"] == ["#ff0000", "#ffffff"]
    assert entries[2]["reason"] == "logo_banner"
    assert entries[3]["reason"] == "unrelated"
    assert entries[4]["reason"] == "low_sharpness"


# 決定論プレフィルタの色計測が「成功（＝単色/グラデではない実商品）」した状態を模擬する注入。
# vision 縮退フォールバックを単独で検証するため、計測は健全（distinct 多・top1 低）にしておく
# （計測失敗 + vision 失敗の二重失敗＝不採用縮退、は別テストで検証する）。
_HEALTHY_STATS = {"pixels": 4096, "distinct": 40, "top1": 0.30, "top3": 0.6}
_HEALTHY_STATS_FN = lambda p: dict(_HEALTHY_STATS)
_HEALTHY_DIMS_FN = lambda p: (1080, 1080)


def test_classify_product_images_falls_back_when_vision_unavailable():
    """vision_call=None（実行不能環境）でも例外を出さず、全採用 + reason=vision_unavailable
    で返す（縮退動作。パイプライン全体は必ず完成する既存設計を踏襲）。計測は健全とする。"""
    paths = ["/tmp/a.jpg", "/tmp/b.jpg"]
    entries = product_images.classify_product_images(
        paths, vision_call=None, color_stats_fn=_HEALTHY_STATS_FN, dims_fn=_HEALTHY_DIMS_FN)
    assert all(e["adopted"] for e in entries)
    assert all(e["reason"] == "vision_unavailable" for e in entries)


def test_classify_product_images_falls_back_when_vision_call_raises():
    def _boom(prompt, paths, timeout_sec=600):
        raise RuntimeError("network broken")
    entries = product_images.classify_product_images(
        ["/tmp/a.jpg"], vision_call=_boom, color_stats_fn=_HEALTHY_STATS_FN, dims_fn=_HEALTHY_DIMS_FN)
    assert entries[0]["adopted"] is True
    assert entries[0]["reason"] == "vision_failed"


def test_classify_product_images_handles_missing_indexes_gracefully():
    """LLM が index を返し漏らしたときは、その画像は縮退で採用（見落としで捨てない）。"""
    def _partial(prompt, paths, timeout_sec=600):
        return {"ok": True, "data": [
            {"index": 1, "category": "product_solo", "sharpness": "high", "dominant_colors": []},
            # index 2 の応答が欠落
        ]}
    entries = product_images.classify_product_images(
        ["/tmp/a.jpg", "/tmp/b.jpg"], vision_call=_partial,
        color_stats_fn=_HEALTHY_STATS_FN, dims_fn=_HEALTHY_DIMS_FN)
    assert entries[0]["adopted"] is True and entries[0]["reason"] is None
    assert entries[1]["adopted"] is True and entries[1]["reason"] == "vision_missing_index"


def test_classify_product_images_normalizes_invalid_hex_colors():
    def _fake(prompt, paths, timeout_sec=600):
        return {"ok": True, "data": [{
            "index": 1, "category": "product_solo", "sharpness": "high",
            "dominant_colors": ["not-a-hex", "#12", "ABCDEF", "#abcdef", "#abcdef"],
        }]}
    entries = product_images.classify_product_images(["/tmp/a.jpg"], vision_call=_fake)
    # 妥当な '#abcdef' / 'ABCDEF' → '#abcdef' に正規化・重複除去、最大3件
    assert entries[0]["dominant_colors"] == ["#abcdef"]


def test_classify_product_images_batches_over_six_images():
    """バッチサイズ6を超える入力でも、複数コールに分けて全画像を分類する。"""
    call_count = {"n": 0}

    def _fake(prompt, paths, timeout_sec=600):
        call_count["n"] += 1
        return {"ok": True, "data": [
            {"index": i + 1, "category": "product_solo", "sharpness": "high", "dominant_colors": []}
            for i in range(len(paths))
        ]}

    paths = ["/tmp/{}.jpg".format(i) for i in range(9)]
    entries = product_images.classify_product_images(paths, vision_call=_fake)
    assert len(entries) == 9
    assert call_count["n"] == 2  # 6 + 3
    assert all(e["adopted"] for e in entries)


def test_save_product_manifest_writes_json(tmp_path):
    entries = [
        {"path": "/tmp/a.jpg", "category": "product_solo", "sharpness": "high",
         "dominant_colors": ["#ff0000"], "adopted": True, "reason": None},
        {"path": "/tmp/b.jpg", "category": "logo_banner", "sharpness": "high",
         "dominant_colors": [], "adopted": False, "reason": "logo_banner"},
    ]
    warns = product_images.save_product_manifest(tmp_path, entries)
    assert warns == []
    manifest = tmp_path / "product_manifest.json"
    assert manifest.exists()
    import json as _json
    payload = _json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert len(payload["entries"]) == 2
    assert payload["entries"][1]["adopted"] is False


# ---------------------------------------------------------------------------
# (i) assign_images_to_shots: reference_spec 有り = 商品shot限定
# ---------------------------------------------------------------------------

def _shot(sid, desc_en=None):
    s = {"id": sid, "caption_jp": ""}
    if desc_en is not None:
        s["reference_visual"] = {"desc_en": desc_en}
    return s


def test_assign_images_to_shots_with_reference_only_uses_product_shots():
    """reference_spec 有り: reference_visual.desc_en が物撮り語を含む shot にのみ image_path。
    それ以外は image_path キー無し（参考の絵をtext-to-videoで作らせる）。"""
    shots = [
        _shot("s1", "a person talks to camera in a bright room"),
        _shot("s2", "close-up of product bottle on shelf"),
        _shot("s3", "hand holding cosmetic jar in bathroom"),
        _shot("s4", "outdoor landscape scene"),
    ]
    result = product_images.assign_images_to_shots(
        shots, ["/p/a.jpg", "/p/b.jpg", "/p/c.jpg"], reference_spec={"shots_ref": []},
    )
    assert "image_path" not in result[0]
    assert result[1]["image_path"] == "/p/a.jpg"
    assert result[2]["image_path"] == "/p/b.jpg"
    assert "image_path" not in result[3]


def test_assign_images_to_shots_no_product_shots_falls_back_to_hook_and_cta():
    """商品shotが検出できない場合はフック+CTAの2点のみに縮退（image_path=None のshotは無し）。"""
    shots = [
        _shot("s1", "a person talks to camera"),
        _shot("s2", "outdoor landscape"),
        _shot("s3", "kitchen scene with vegetables"),
    ]
    result = product_images.assign_images_to_shots(
        shots, ["/p/a.jpg", "/p/b.jpg"], reference_spec={"shots_ref": []},
    )
    assert result[0]["image_path"] == "/p/a.jpg"
    assert "image_path" not in result[1]
    assert result[2]["image_path"] == "/p/b.jpg"


def test_assign_images_to_shots_reference_none_is_backward_compat():
    """reference_spec=None は従来の hook/CTA/middle 均等ロジックを維持（旧テストとの互換）。"""
    shots = [_shot("s{}".format(i + 1)) for i in range(5)]
    images = ["p0.jpg", "p1.jpg", "p2.jpg", "p3.jpg", "p4.jpg"]
    result = product_images.assign_images_to_shots(shots, images)
    assert result[0]["image_path"] == "p0.jpg"  # hook
    assert result[4]["image_path"] == "p1.jpg"  # CTA
    assert result[1]["image_path"] == "p2.jpg"
    assert result[2]["image_path"] == "p3.jpg"
    assert result[3]["image_path"] == "p4.jpg"


def test_assign_images_to_shots_uses_shots_ref_by_position_when_shot_has_no_reference_visual():
    """shot に reference_visual が無く、shots_ref と shot 数が一致する場合、
    位置対応で shots_ref[i] を1件だけ参照する（codex-review P1: 全shots_refをどのshotにも
    適用しない）。has_product_logo=True の shots_ref に対応する index の shot だけが
    商品shot扱いになる。"""
    shots = [{"id": "s1"}, {"id": "s2"}, {"id": "s3"}]
    reference_spec = {
        "shots_ref": [
            {"start": 0, "end": 2, "visual_desc_en": "person face", "has_product_logo": False},
            {"start": 2, "end": 4, "visual_desc_en": "product shelf", "has_product_logo": True},
            {"start": 4, "end": 6, "visual_desc_en": "landscape", "has_product_logo": False},
        ],
    }
    indices = product_images.product_shot_indices(shots, reference_spec)
    assert indices == [1]  # index 1 のみ商品shot（無関係shotに商品画像が撒かれない）

    result = product_images.assign_images_to_shots(
        shots, ["/p/a.jpg", "/p/b.jpg"], reference_spec=reference_spec,
    )
    assert "image_path" not in result[0]
    assert result[1]["image_path"] == "/p/a.jpg"
    assert "image_path" not in result[2]


def test_assign_images_to_shots_no_alignment_falls_back_to_hook_cta_when_shots_ref_len_mismatch():
    """shots_ref と shot 数が食い違うときは位置対応不能。商品shot=0として縮退（フック+CTA）。"""
    shots = [{"id": "s1"}, {"id": "s2"}, {"id": "s3"}]  # 3 shots
    reference_spec = {
        "shots_ref": [
            {"start": 0, "end": 5, "visual_desc_en": "product close up", "has_product_logo": True},
        ],  # 1 shots_ref のみ（不一致）
    }
    indices = product_images.product_shot_indices(shots, reference_spec)
    assert indices == []
    result = product_images.assign_images_to_shots(
        shots, ["/p/a.jpg", "/p/b.jpg"], reference_spec=reference_spec,
    )
    # 縮退: フック+CTA
    assert result[0]["image_path"] == "/p/a.jpg"
    assert "image_path" not in result[1]
    assert result[2]["image_path"] == "/p/b.jpg"


def test_product_shot_indices_returns_only_product_shot_positions():
    shots = [
        _shot("s1", "a person talks"),
        _shot("s2", "close-up of product bottle"),
        _shot("s3", "landscape"),
        _shot("s4", "hand holding jar"),
    ]
    assert product_images.product_shot_indices(shots, {"shots_ref": []}) == [1, 3]


# ---------------------------------------------------------------------------
# (j) プロンプト序列: product_block は「優先」「主役にしたまま」「悪い例」を主張しない
# ---------------------------------------------------------------------------

def test_product_block_prompt_no_priority_or_hero_claims():
    """KR1 実証: product_block.txt から「優先」の断定・「主役にしたまま」「悪い例」
    等の商品最優先を助長する語彙が排除されていること。逆に参考動画への従属を示す文言
    （参考shot / 従属 / 差し替え / 参考動画）が含まれること。"""
    from pipeline.config import project_root
    text = (project_root() / "pipeline" / "prompts" / "product_block.txt").read_text(encoding="utf-8")
    forbidden = ["最優先", "こちらを優先", "主役にしたまま", "悪い例"]
    for w in forbidden:
        assert w not in text, "product_block.txt に禁止語 '{}' が残っている".format(w)
    # 従属性・参考ベースの文言が含まれていること
    assert "参考動画" in text
    assert ("参考shot" in text) or ("被写体スロット" in text) or ("差し替え" in text)


def test_reference_ttp_block_declares_top_priority():
    """KR1 実証: reference_ttp_block.txt に「最上位」「他ブロックと矛盾したら本ブロック優先」
    に相当する宣言が入っていること。"""
    from pipeline.config import project_root
    text = (project_root() / "pipeline" / "prompts" / "reference_ttp_block.txt").read_text(encoding="utf-8")
    assert "最上位" in text


# ---------------------------------------------------------------------------
# (j) 決定論プレフィルタ: 単色/グラデ装飾背景・極端アスペクトを vision 前段で除外
#     （診断 P1-5 / #4/#11 — LPの純ピンク背景 001.png が i2v seed を汚染した対策）
# ---------------------------------------------------------------------------

def test_is_monochrome_or_gradient_flags_low_distinct():
    # 001.png 実測相当（distinct=3 は極端に少数色＝単色/緩いグラデ）
    stats = {"pixels": 4096, "distinct": 3, "top1": 0.717, "top3": 1.0}
    assert product_images.is_monochrome_or_gradient(stats) is True


def test_is_monochrome_or_gradient_flags_near_solid_flat_plate():
    # 単一色が画面の大半（>=0.85）を占め、かつ色種も乏しい（distinct<16）ならベタ/グラデ板
    stats = {"pixels": 4096, "distinct": 10, "top1": 0.90, "top3": 0.96}
    assert product_images.is_monochrome_or_gradient(stats) is True


def test_is_monochrome_or_gradient_keeps_small_product_on_plain_background():
    # codex-review P1: 白/無地スタジオ背景に小さく写る正規商品は top1 が高くても
    # 色種が豊富（distinct>=16）なので除外しない（商品の輪郭・陰影・文字で色数が増える）。
    stats = {"pixels": 4096, "distinct": 40, "top1": 0.90, "top3": 0.94}
    assert product_images.is_monochrome_or_gradient(stats) is False


def test_is_monochrome_or_gradient_keeps_rich_product_image():
    # 002〜006 実測相当（distinct>=29, top1<=0.65）は商品画像として通過させる
    for stats in (
        {"distinct": 29, "top1": 0.567},
        {"distinct": 68, "top1": 0.651},
        {"distinct": 48, "top1": 0.271},
    ):
        assert product_images.is_monochrome_or_gradient(stats) is False


def test_is_monochrome_or_gradient_none_stats_is_conservative_keep():
    # 計測不能（None）は判定不能＝除外しない（保守側でvisionへ委ねる）
    assert product_images.is_monochrome_or_gradient(None) is False


def test_is_extreme_aspect_flags_banner_shapes():
    assert product_images.is_extreme_aspect(1200, 300) is True   # 4:1 横長バナー
    assert product_images.is_extreme_aspect(300, 1200) is True   # 1:4 縦長帯
    assert product_images.is_extreme_aspect(2000, 600) is True   # 3.33:1


def test_is_extreme_aspect_keeps_normal_portrait_and_landscape():
    assert product_images.is_extreme_aspect(800, 1000) is False  # 001 実寸(0.8)
    assert product_images.is_extreme_aspect(750, 1230) is False  # 002 実寸
    assert product_images.is_extreme_aspect(1080, 1920) is False # 9:16
    assert product_images.is_extreme_aspect(0, 100) is False     # 不正は除外しない


def test_deterministic_prefilter_with_injected_measures():
    stats_map = {
        "/p/pink.png": {"distinct": 3, "top1": 0.72},   # 単色/グラデ → 除外
        "/p/banner.png": {"distinct": 40, "top1": 0.4}, # 色は豊富だがアスペクト極端 → 除外
        "/p/product.png": {"distinct": 50, "top1": 0.5},# 通過
    }
    dims_map = {
        "/p/pink.png": (800, 1000),
        "/p/banner.png": (1600, 400),
        "/p/product.png": (750, 1200),
    }
    out = product_images.deterministic_prefilter(
        list(stats_map.keys()),
        color_stats_fn=lambda p: stats_map[p],
        dims_fn=lambda p: dims_map[p],
    )
    assert out["/p/pink.png"]["excluded"] is True
    assert out["/p/pink.png"]["reason"] == "monochrome_or_gradient"
    assert out["/p/banner.png"]["excluded"] is True
    assert out["/p/banner.png"]["reason"] == "extreme_aspect"
    assert out["/p/product.png"]["excluded"] is False


def test_classify_product_images_prefilter_excludes_and_skips_vision():
    """前段で除外された画像は adopted=False + 決定論 reason で、visionには回さない。"""
    vision_seen = {"paths": None, "calls": 0}

    def _fake_vision(prompt, paths, timeout_sec=600):
        vision_seen["paths"] = list(paths)
        vision_seen["calls"] += 1
        return {"ok": True, "data": [
            {"index": i + 1, "category": "product_solo", "sharpness": "high", "dominant_colors": []}
            for i in range(len(paths))
        ]}

    paths = ["/p/pink.png", "/p/prod.png"]
    stats_map = {"/p/pink.png": {"distinct": 3, "top1": 0.72}, "/p/prod.png": {"distinct": 50, "top1": 0.5}}
    entries = product_images.classify_product_images(
        paths, vision_call=_fake_vision,
        color_stats_fn=lambda p: stats_map[p], dims_fn=lambda p: (800, 1000),
    )
    # 入力順は保たれる
    assert [e["path"] for e in entries] == paths
    assert entries[0]["adopted"] is False
    assert entries[0]["reason"] == "monochrome_or_gradient"
    assert entries[1]["adopted"] is True
    # visionには除外画像を渡していない（コスト節約）
    assert vision_seen["paths"] == ["/p/prod.png"]
    assert vision_seen["calls"] == 1


def test_classify_product_images_all_prefiltered_skips_vision_entirely():
    def _boom_vision(prompt, paths, timeout_sec=600):
        raise AssertionError("visionは呼ばれてはならない")

    entries = product_images.classify_product_images(
        ["/p/a.png", "/p/b.png"], vision_call=_boom_vision,
        color_stats_fn=lambda p: {"distinct": 2, "top1": 0.99}, dims_fn=lambda p: (900, 1000),
    )
    assert all(e["adopted"] is False for e in entries)
    assert all(e["reason"] == "monochrome_or_gradient" for e in entries)


def test_classify_product_images_prefilter_disabled_is_backward_compat():
    """prefilter_enabled=False なら従来どおり全て vision に回る。"""
    def _fake_vision(prompt, paths, timeout_sec=600):
        return {"ok": True, "data": [
            {"index": i + 1, "category": "product_solo", "sharpness": "high", "dominant_colors": []}
            for i in range(len(paths))
        ]}
    entries = product_images.classify_product_images(
        ["/p/pink.png"], vision_call=_fake_vision, prefilter_enabled=False,
        color_stats_fn=lambda p: {"distinct": 2, "top1": 0.99},
    )
    assert entries[0]["adopted"] is True


def test_measure_color_stats_real_pink_lp_background_is_excluded():
    """KR1 実測: 実物 001.png（LP純ピンクグラデ背景）が実 ffmpeg 計測で除外される。

    bin/ffmpeg と実ファイルが無い環境ではスキップ（CI hermetic 環境保護）。"""
    from pipeline.config import project_root
    root = project_root()
    ffmpeg = root / "bin" / "ffmpeg"
    pink = root / "projects" / "p_20260724153643_b436cf49" / "product" / "001.png"
    product = root / "projects" / "p_20260724153643_b436cf49" / "product" / "006.png"
    if not ffmpeg.exists() or not pink.exists() or not product.exists():
        pytest.skip("bin/ffmpeg または実物商品画像が無い環境")
    pink_stats = product_images.measure_color_stats(str(pink), ffmpeg_bin=str(ffmpeg))
    prod_stats = product_images.measure_color_stats(str(product), ffmpeg_bin=str(ffmpeg))
    assert pink_stats is not None and prod_stats is not None
    assert product_images.is_monochrome_or_gradient(pink_stats) is True
    assert product_images.is_monochrome_or_gradient(prod_stats) is False

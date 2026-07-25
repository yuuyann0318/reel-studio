# -*- coding: utf-8 -*-
"""商品LP(ランディングページ)からのアフィリエイト用商品画像自動取得。

美容系商品アフィリ動画モードの入口。商品LPのURLからOGP画像/img画像を抽出し、
解像度フィルタ・重複排除を経てdest_dirへ連番保存する。local_dir(ユーザーが
用意した手元画像フォルダ)があれば常に先頭優先で使う。

実ネットワークアクセスはデフォルトのfetcher/proberに限定し、テストでは
fetcher/proberを注入して完全にモック可能にする（実ネットワーク禁止環境でも
ロジックを検証できるようにするため）。

Python 3.9 互換構文のみ。stdlibのみ使用（requests等の外部依存は禁止）。
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import socket
import subprocess
import urllib.error
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

DEFAULT_MAX_IMAGES = 6
DEFAULT_MIN_SHORT_SIDE = 400
DEFAULT_TIMEOUT_SEC = 15
DEFAULT_USER_AGENT = "Mozilla/5.0 (compatible; HiggsfieldAutoReel/1.0; +product-image-fetcher)"
DEFAULT_MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024  # 10MB: 画像1枚あたりのダウンロード上限
DEFAULT_MAX_HTML_BYTES = 2 * 1024 * 1024  # 2MB: 商品LP HTMLのダウンロード上限
_MAX_REDIRECTS = 5

_LOCAL_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")
_ALLOWED_SAVE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp")


# ---------------------------------------------------------------------------
# URL安全性チェック（SSRF対策: 私有/ループバック系ホストへのアクセスを拒否）
# ---------------------------------------------------------------------------

def _has_control_or_ws_chars(url):
    """URL文字列にHTTP禁止の制御文字/空白が含まれるかを判定する。

    LPが Liquid テンプレート等でレンダリング失敗した場合、埋め込まれたURLに
    半角スペースや改行が混じることがある(例: `pd[em]=Liquid error: internal`)。
    urllib.request.Request は制御文字を含むURLを ValueError で拒否するため、
    fetch段までは通せない=フェッチ失敗の警告として表示され「画像が読み込めない」
    と受け取られるユーザーエラーの原因になる。事前に検知して静かにスキップする
    (実質フェッチ不可能なURL=warningsに転記する価値がないため)。
    """
    if not url or not isinstance(url, str):
        return False
    for ch in url:
        code = ord(ch)
        if code < 0x20 or code == 0x7F or ch == " ":
            return True
    return False


def _is_safe_url(url):
    """http/https以外、または私有系(loopback/private/link-local等)ホストなら False。

    DNS解決は行わない（ホスト名文字列/IPリテラルのみの判定。実ネットワーク禁止
    環境でも決定論的に判定できるようにするため）。制御文字/半角スペースを含む
    URL(HTTPで送出不可)も False とする。
    """
    if not url or not isinstance(url, str):
        return False
    if _has_control_or_ws_chars(url):
        return False
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    hostname = parsed.hostname
    if not hostname:
        return False
    host_l = hostname.lower()
    if host_l == "localhost" or host_l.endswith(".localhost") or host_l.endswith(".local"):
        return False

    try:
        ip = ipaddress.ip_address(host_l.strip("[]"))
    except ValueError:
        ip = None

    if ip is not None:
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return False
    return True


def _is_ip_unsafe(ip):
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _is_safe_resolved_host(hostname):
    """ホスト名を実際にDNS解決し、解決先IPが私有/ループバック系でないかを検査する。

    DNS rebinding対策: `_is_safe_url()` はURL文字列上のホスト名/IPリテラルしか見ないため、
    一見安全なホスト名が実際には私有IP（例: 169.254.169.254 のようなメタデータサービス）へ
    解決される攻撃を防げない。フェッチ直前にこの関数で再解決し、解決先IPのいずれか1つでも
    私有系ならFalseを返して拒否する。解決に失敗した場合も安全側に倒してFalseを返す。
    """
    if not hostname:
        return False
    try:
        infos = socket.getaddrinfo(hostname, None)
    except Exception:
        return False
    if not infos:
        return False
    for info in infos:
        raw_addr = info[4][0]
        addr = raw_addr.split("%")[0]  # IPv6 zone id (fe80::1%en0 等) を除去
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return False
        if _is_ip_unsafe(ip):
            return False
    return True


class _SafeRedirectHandler(HTTPRedirectHandler):
    """各リダイレクトホップの遷移先URLへ `_is_safe_url`/DNS解決チェックを再適用するハンドラ。

    標準の `HTTPRedirectHandler` は追従先URLを一切検証しないため、初回URLが安全でも
    301/302で私有IP（例: 127.0.0.1, 169.254.169.254）へ誘導されるとSSRFが成立してしまう
    （リダイレクト追従によるSSRF対策の抜け）。`redirect_request()` をオーバーライドし、
    各ホップで拒否判定を行う。不合格なら `urllib.error.URLError` を送出し、呼び出し元の
    try/exceptでwarningsへ記録される。
    """

    max_redirections = _MAX_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _is_safe_url(newurl):
            raise urllib.error.URLError(
                "リダイレクト先が不正な(私有/非http系)URLのため拒否しました: {!r}".format(newurl)
            )
        if not _is_safe_resolved_host(urlparse(newurl).hostname):
            raise urllib.error.URLError(
                "リダイレクト先ホストのDNS解決結果が私有IPのため拒否しました: {!r}".format(newurl)
            )
        return HTTPRedirectHandler.redirect_request(self, req, fp, code, msg, headers, newurl)


class DownloadTooLargeError(Exception):
    """レスポンスサイズが上限を超えた場合に送出する（呼び出し元で捕捉してwarningsに記録する）。"""


# ---------------------------------------------------------------------------
# HTML解析: og:image/twitter:image/img抽出
# ---------------------------------------------------------------------------

def _pick_best_srcset(srcset_value):
    """srcset属性から最大解像度候補のURLを選ぶ（Nw降順 > Nx降順 > 記載順）。"""
    best_url = None
    best_score = -1.0
    for part in srcset_value.split(","):
        part = part.strip()
        if not part:
            continue
        pieces = part.split()
        candidate_url = pieces[0].strip()
        score = 0.0
        if len(pieces) > 1:
            desc = pieces[1].strip()
            try:
                if desc.endswith("w"):
                    score = float(desc[:-1])
                elif desc.endswith("x"):
                    score = float(desc[:-1]) * 1000.0
            except ValueError:
                score = 0.0
        if score >= best_score:
            best_score = score
            best_url = candidate_url
    return best_url


class _ProductPageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.og_image = None
        self.twitter_image = None
        self.og_title = None
        self.title = None
        self.img_candidates = []
        self._in_title = False
        self._title_parts = []

    def handle_starttag(self, tag, attrs):
        attrs_d = {k.lower(): v for k, v in attrs if k}
        tag_l = tag.lower()
        if tag_l == "meta":
            prop = (attrs_d.get("property") or attrs_d.get("name") or "").strip().lower()
            content = attrs_d.get("content")
            if content:
                content = content.strip()
                if prop == "og:image" and not self.og_image:
                    self.og_image = content
                elif prop == "twitter:image" and not self.twitter_image:
                    self.twitter_image = content
                elif prop == "og:title" and not self.og_title:
                    self.og_title = content
        elif tag_l == "title":
            self._in_title = True
        elif tag_l == "img":
            srcset = attrs_d.get("data-srcset") or attrs_d.get("srcset")
            best_from_srcset = _pick_best_srcset(srcset) if srcset else None
            src = attrs_d.get("data-src") or attrs_d.get("src")
            candidate = best_from_srcset or src
            if candidate:
                self.img_candidates.append(candidate.strip())

    def handle_endtag(self, tag):
        if tag.lower() == "title":
            self._in_title = False
            if self.title is None:
                self.title = "".join(self._title_parts).strip()

    def handle_data(self, data):
        if self._in_title:
            self._title_parts.append(data)


def _is_excluded_image_url(url):
    if not url:
        return True
    if url.startswith("data:"):
        return True
    lower = url.lower().split("?")[0].split("#")[0]
    if lower.endswith(".svg"):
        return True
    if "favicon" in lower:
        return True
    return False


def extract_page_info(html_text, base_url):
    """HTML文字列から {"title": str, "image_urls": [str,...]} を抽出する。

    優先順位: og:image → twitter:image → <img>のsrc/data-src/srcset(最大解像度)を文書順。
    相対URLはbase_urlでurljoin解決。data:URI/.svg/favicon系は除外。重複は除去(先勝ち)。
    商品名はog:title→<title>の順。
    """
    parser = _ProductPageParser()
    try:
        parser.feed(html_text or "")
        parser.close()
    except Exception:
        pass

    raw_urls = []
    if parser.og_image:
        raw_urls.append(parser.og_image)
    if parser.twitter_image:
        raw_urls.append(parser.twitter_image)
    raw_urls.extend(parser.img_candidates)

    seen = set()
    image_urls = []
    for raw in raw_urls:
        if not raw:
            continue
        raw_stripped = raw.strip()
        # LPが Liquid テンプレのエラー文字列(例: "Liquid error: internal")を
        # そのままレンダリングしていると、img src 属性値の中に半角スペース/改行が
        # 混ざる。urllib.request.Request は制御文字を含むURLを ValueError で拒否
        # するため、事前に弾いて fetch 側でノイジーなwarningを出さない
        # (extract_page_info の段で捨てるので、呼び出し側は完全に気付かない)。
        if _has_control_or_ws_chars(raw_stripped):
            continue
        try:
            resolved = urljoin(base_url, raw_stripped)
        except Exception:
            continue
        if _has_control_or_ws_chars(resolved):
            continue
        if _is_excluded_image_url(resolved):
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        image_urls.append(resolved)

    title = parser.og_title or parser.title or ""
    return {"title": title, "image_urls": image_urls}


# ---------------------------------------------------------------------------
# 既定fetcher/prober（実ネットワーク/ffprobe呼び出し。テストでは注入で差し替え）
# ---------------------------------------------------------------------------

def _build_default_fetcher(cfg=None):
    """cfg(timeout_sec/max_download_bytes)を反映したデフォルトfetcherを構築する。

    SSRF対策: 送信直前にDNS解決ベースの私有IPチェック(`_is_safe_resolved_host`)を行い、
    `build_opener(_SafeRedirectHandler)` でリダイレクト追従の各ホップにも同じチェックを効かせる。
    ダウンロードサイズ無制限対策: `resp.read(max_bytes + 1)` で上限超過を検知し
    `DownloadTooLargeError` を送出する（呼び出し元で捕捉してwarningsに記録・破棄する）。
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    timeout_sec = cfg.get("timeout_sec", DEFAULT_TIMEOUT_SEC)
    max_bytes = cfg.get("max_download_bytes", DEFAULT_MAX_DOWNLOAD_BYTES)

    def _fetch(url):
        if not _is_safe_resolved_host(urlparse(url).hostname):
            raise urllib.error.URLError(
                "ホストのDNS解決結果が私有IPのため拒否しました: {!r}".format(url)
            )
        req = Request(url, headers={"User-Agent": DEFAULT_USER_AGENT})
        opener = build_opener(_SafeRedirectHandler)
        with opener.open(req, timeout=timeout_sec) as resp:  # noqa: S310 (許可済みの限定fetch)
            data = resp.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise DownloadTooLargeError(
                    "レスポンスサイズが上限({} bytes)を超えました: {!r}".format(max_bytes, url)
                )
            return data

    return _fetch


def _default_fetcher(url):
    """後方互換用の既定fetcher（cfg無し。timeout_sec/max_download_bytesは既定値を使う）。"""
    return _build_default_fetcher(None)(url)


def _default_prober(image_path, ffprobe_bin):
    cmd = [
        ffprobe_bin,
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "csv=s=x:p=0",
        image_path,
    ]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
    except Exception:
        return None
    out = proc.stdout.decode("utf-8", "replace").strip().splitlines()
    if not out:
        return None
    try:
        w_str, h_str = out[0].strip().split("x")
        return int(w_str), int(h_str)
    except Exception:
        return None


def _guess_ext(url):
    try:
        path = urlparse(url).path
    except Exception:
        path = ""
    ext = os.path.splitext(path)[1].lower()
    if ext in _ALLOWED_SAVE_EXTS:
        return ".jpg" if ext == ".jpeg" else ext
    return ".jpg"


# ---------------------------------------------------------------------------
# メイン: 商品画像の収集
# ---------------------------------------------------------------------------

def collect_product_images(url, dest_dir, cfg=None, local_dir=None, fetcher=None, prober=None):
    """商品LP(url)から画像を収集し、dest_dirへ連番保存する。

    local_dirにjpg/png/webp画像があれば先頭優先でコピーし、残り枠をLPから補う。
    短辺がmin_image_short_side(既定400)px未満の画像・sha256重複は除外し、
    max_images(既定6)枚を上限に打ち切る。

    例外は握りつぶしてwarningsに記録し、呼び出し元を落とさない（常に有効なdictを返す）。

    Returns:
        {"name": str, "url": str, "images": [{"path","source","width","height"}...],
         "warnings": [str,...]}
    """
    cfg = cfg or {}
    warnings = []
    name = ""
    collected = []
    seen_hashes = set()

    max_images = cfg.get("max_images", DEFAULT_MAX_IMAGES) if isinstance(cfg, dict) else DEFAULT_MAX_IMAGES
    min_short_side = (
        cfg.get("min_image_short_side", DEFAULT_MIN_SHORT_SIDE) if isinstance(cfg, dict) else DEFAULT_MIN_SHORT_SIDE
    )
    ffprobe_bin = (cfg.get("ffprobe_bin") or "ffprobe") if isinstance(cfg, dict) else "ffprobe"
    max_html_bytes = cfg.get("max_html_bytes", DEFAULT_MAX_HTML_BYTES) if isinstance(cfg, dict) else DEFAULT_MAX_HTML_BYTES
    max_image_bytes = (
        cfg.get("max_download_bytes", DEFAULT_MAX_DOWNLOAD_BYTES) if isinstance(cfg, dict) else DEFAULT_MAX_DOWNLOAD_BYTES
    )

    # fetcher未注入時は、cfg(timeout_sec/max_download_bytes)を反映した既定fetcherを使う
    # （実ネットワークアクセス + SSRF対策込み。テストでは常にfetcherを注入するため通らない経路）。
    fetch = fetcher or _build_default_fetcher(cfg)
    probe = prober or (lambda path: _default_prober(path, ffprobe_bin))

    try:
        os.makedirs(dest_dir, exist_ok=True)
    except Exception as exc:
        warnings.append("保存先ディレクトリの作成に失敗しました: {}".format(exc))
        return {"name": name, "url": url, "images": [], "warnings": warnings}

    idx = 1

    def _save_and_probe(data, ext, source):
        nonlocal idx
        if not data:
            return None
        digest = hashlib.sha256(data).hexdigest()
        if digest in seen_hashes:
            return None
        path = os.path.join(dest_dir, "{:03d}{}".format(idx, ext))
        try:
            with open(path, "wb") as f:
                f.write(data)
        except Exception as exc:
            warnings.append("画像の保存に失敗しました: {}".format(exc))
            return None
        dims = None
        try:
            dims = probe(path)
        except Exception as exc:
            warnings.append("画像サイズの判定に失敗しました: {} ({})".format(path, exc))
        if not dims:
            try:
                os.remove(path)
            except Exception:
                pass
            return None
        width, height = dims
        if min(width, height) < min_short_side:
            try:
                os.remove(path)
            except Exception:
                pass
            return None
        seen_hashes.add(digest)
        idx += 1
        return {"path": path, "source": source, "width": width, "height": height}

    # 1. local_dir 優先
    if local_dir:
        try:
            entries = sorted(os.listdir(local_dir)) if os.path.isdir(local_dir) else []
        except Exception as exc:
            entries = []
            warnings.append("local_dirの読み込みに失敗しました: {}".format(exc))
        for fname in entries:
            if len(collected) >= max_images:
                break
            ext = os.path.splitext(fname)[1].lower()
            if ext not in _LOCAL_IMAGE_EXTS:
                continue
            src_path = os.path.join(local_dir, fname)
            try:
                with open(src_path, "rb") as f:
                    data = f.read()
            except Exception as exc:
                warnings.append("local_dir画像の読み込みに失敗しました: {} ({})".format(fname, exc))
                continue
            save_ext = ".jpg" if ext == ".jpeg" else ext
            item = _save_and_probe(data, save_ext, "local")
            if item:
                collected.append(item)

    # 2. LPから抽出・DL
    if len(collected) < max_images:
        if not _is_safe_url(url):
            warnings.append("不正な(私有/非http系)URLのため商品LPの取得をスキップしました: {!r}".format(url))
        else:
            try:
                page_bytes = fetch(url)
                if len(page_bytes or b"") > max_html_bytes:
                    # ダウンロードサイズ無制限対策: LP HTMLが上限を超えたら破棄しparse自体を行わない
                    # （fetcher実装(既定/注入どちらも)がサイズ制限を持たない場合の防御）。
                    warnings.append(
                        "商品LPのサイズが上限({} bytes)を超えたため破棄しました: {} bytes".format(
                            max_html_bytes, len(page_bytes)
                        )
                    )
                else:
                    page_text = (
                        page_bytes.decode("utf-8", "replace") if isinstance(page_bytes, bytes) else str(page_bytes)
                    )
                    info = extract_page_info(page_text, url)
                    if info.get("title"):
                        name = info["title"]
                    for img_url in info.get("image_urls", []):
                        if len(collected) >= max_images:
                            break
                        if not _is_safe_url(img_url):
                            warnings.append("不正な(私有/非http系)画像URLをスキップしました: {!r}".format(img_url))
                            continue
                        try:
                            img_bytes = fetch(img_url)
                        except Exception as exc:
                            warnings.append("画像の取得に失敗しました: {} ({})".format(img_url, exc))
                            continue
                        if len(img_bytes or b"") > max_image_bytes:
                            warnings.append(
                                "画像サイズが上限({} bytes)を超えたため破棄しました: {} ({} bytes)".format(
                                    max_image_bytes, img_url, len(img_bytes)
                                )
                            )
                            continue
                        item = _save_and_probe(img_bytes, _guess_ext(img_url), "lp")
                        if item:
                            collected.append(item)
            except Exception as exc:
                warnings.append("商品LPの取得に失敗しました: {} ({})".format(url, exc))

    return {"name": name, "url": url, "images": collected, "warnings": warnings}


# ---------------------------------------------------------------------------
# 決定論フィルタ（vision分類の前段。単色/グラデ装飾背景・極端アスペクトを機械除外）
# ---------------------------------------------------------------------------
#
# 診断（/tmp/ttp_gap_diagnosis.md P1-5, #4/#11）で判明した実害:
#   商品LPの「純ピンクグラデーション装飾背景」 001.png が商品画像として採用され、
#   i2v の s1 起点シードに食われて冒頭にピンク薄膜が載った。vision分類だけに頼ると
#   稀に取りこぼす（LLMが装飾背景を product_solo と誤答しうる）ため、vision の前段に
#   決定論的なフィルタを1枚噛ませる:
#     (a) 色ヒストグラム集中度 = 画面が少数の量子化色に潰れている（単色/緩いグラデ）
#     (b) 極端なアスペクト比 = バナー形状（横長/縦長すぎる帯）
#   実測（projects/p_20260724153643_b436cf49/product/*.png, grid=64, quant_bits=3）:
#     001(ピンクグラデ背景): distinct=3, top1=0.717  → (a)で除外
#     002〜006(実商品LP画像): distinct=29〜68, top1<=0.651 → 通過
#   distinct（量子化後の色種数）が単色/グラデと実写商品を最も明瞭に分離する（3 vs >=29）。

DEFAULT_PREFILTER_GRID = 64          # 色ヒストグラム計測用の縮小グリッド（64x64=4096px）
DEFAULT_PREFILTER_QUANT_BITS = 3     # 1chあたり3bit=8階調へ量子化（最大512バケット）
DEFAULT_PREFILTER_MIN_DISTINCT = 9   # 量子化色種数がこれ未満なら単色/グラデ扱い（001=3を除外）
DEFAULT_PREFILTER_SOLID_TOP1 = 0.85  # 単一量子化色が画面の85%以上なら near-solid 候補
DEFAULT_PREFILTER_SOLID_MAX_DISTINCT = 16  # near-solid はこの色種数未満のときだけ除外（下記参照）
DEFAULT_PREFILTER_ASPECT_MAX = 3.0   # max(w/h, h/w) がこれ以上ならバナー形状として除外


def _resolve_default_ffmpeg_bin():
    """既定 ffmpeg バイナリ（bin/ffmpeg）を project_root 基準で解決する（遅延import）。"""
    try:
        from pipeline.config import project_root
        return str(project_root() / "bin" / "ffmpeg")
    except Exception:
        return "ffmpeg"


def measure_color_stats(image_path, ffmpeg_bin=None, grid=DEFAULT_PREFILTER_GRID,
                        quant_bits=DEFAULT_PREFILTER_QUANT_BITS, timeout_sec=15):
    """画像を grid×grid の raw RGB へ縮小し、量子化色ヒストグラムの集中度を計測する。

    ffmpeg で rawvideo(rgb24) を stdout に吐かせ、純Python(stdlib)で量子化・集計する。
    PIL/numpy 等の外部依存は追加しない（本モジュールは stdlib のみの方針）。

    Returns: {"pixels":int, "distinct":int, "top1":float, "top3":float} または
        計測不能時（ファイル不在・ffmpeg失敗・空出力）は None（=判定不能＝除外しない）。
    """
    ffmpeg_bin = ffmpeg_bin or _resolve_default_ffmpeg_bin()
    g = max(8, int(grid))
    cmd = [
        str(ffmpeg_bin), "-v", "error", "-i", str(image_path),
        "-vf", "scale={g}:{g}".format(g=g), "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
    ]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout_sec)
    except Exception:
        return None
    data = proc.stdout or b""
    n = len(data) // 3
    if n <= 0:
        return None
    shift = 8 - max(1, min(8, int(quant_bits)))
    buckets = {}
    for i in range(0, n * 3, 3):
        key = ((data[i] >> shift), (data[i + 1] >> shift), (data[i + 2] >> shift))
        buckets[key] = buckets.get(key, 0) + 1
    counts_desc = sorted(buckets.values(), reverse=True)
    top1 = counts_desc[0] / float(n)
    top3 = sum(counts_desc[:3]) / float(n)
    return {"pixels": n, "distinct": len(buckets), "top1": round(top1, 4), "top3": round(top3, 4)}


def is_monochrome_or_gradient(stats, min_distinct=DEFAULT_PREFILTER_MIN_DISTINCT,
                              solid_top1_threshold=DEFAULT_PREFILTER_SOLID_TOP1,
                              solid_max_distinct=DEFAULT_PREFILTER_SOLID_MAX_DISTINCT):
    """color stats（measure_color_stats の戻り）から単色/グラデ装飾背景かを判定する（純関数）。

    除外条件（いずれか成立で True）:
      1. distinct（量子化色種数） < min_distinct … 少数色に潰れている=単色/緩いグラデ
         （実測: 001ピンクグラデ背景=3 に対し実商品画像は 29〜68。極めて明瞭に分離する）
      2. top1（最頻量子化色の占有率） >= solid_top1_threshold **かつ** distinct < solid_max_distinct
         … ほぼ単一色の面で、色数も乏しい＝ベタ/グラデ板。
         ★codex-review P1 対策: top1 だけで除外すると、白/無地スタジオ背景に小さく写る
         正規の product_solo（product が画面の15%未満だと top1>0.85 になりうる）まで
         誤って落としてしまう。実際の商品は輪郭・陰影・文字で色種が増える（distinct が高い）ため、
         「単一色が支配的 かつ 色種も乏しい」ときに限って near-solid とみなす。
    stats が None（計測不能）なら False（判定不能＝除外しない・保守側）。
    """
    if not isinstance(stats, dict):
        return False
    distinct = stats.get("distinct")
    top1 = stats.get("top1")
    if isinstance(distinct, int) and distinct < int(min_distinct):
        return True
    if (
        isinstance(top1, (int, float)) and float(top1) >= float(solid_top1_threshold)
        and isinstance(distinct, int) and distinct < int(solid_max_distinct)
    ):
        return True
    return False


def is_extreme_aspect(width, height, max_ratio=DEFAULT_PREFILTER_ASPECT_MAX):
    """バナー形状（横長/縦長すぎ）かを判定する（純関数）。

    max(w/h, h/w) >= max_ratio で True。width/height が不正なら False（判定不能）。
    """
    try:
        w = float(width)
        h = float(height)
    except (TypeError, ValueError):
        return False
    if w <= 0 or h <= 0:
        return False
    ratio = max(w / h, h / w)
    return ratio >= float(max_ratio)


def deterministic_prefilter(
    image_paths, ffmpeg_bin=None, color_stats_fn=None, dims_fn=None, cfg=None,
):
    """vision分類の前段。単色/グラデ装飾背景・極端アスペクト比を決定論的に除外判定する。

    color_stats_fn / dims_fn は DI（テストで注入）。未指定時は実 ffmpeg/ffprobe を使う。
    計測不能（None）の画像は除外しない（保守側＝vision へ委ねる）。

    Returns: dict[path] -> {"excluded":bool, "reason":str|None, "stats":dict|None,
        "width":int|None, "height":int|None}
    """
    pf_cfg = {}
    if isinstance(cfg, dict):
        pf_cfg = (cfg.get("product_images") or {}).get("prefilter") or {}
    min_distinct = pf_cfg.get("min_distinct", DEFAULT_PREFILTER_MIN_DISTINCT)
    solid_top1 = pf_cfg.get("solid_top1", DEFAULT_PREFILTER_SOLID_TOP1)
    solid_max_distinct = pf_cfg.get("solid_max_distinct", DEFAULT_PREFILTER_SOLID_MAX_DISTINCT)
    aspect_max = pf_cfg.get("aspect_max_ratio", DEFAULT_PREFILTER_ASPECT_MAX)
    ffprobe_bin = (cfg.get("ffprobe_bin") if isinstance(cfg, dict) else None) or "ffprobe"

    stats_fn = color_stats_fn or (lambda p: measure_color_stats(p, ffmpeg_bin=ffmpeg_bin))
    dims_fn = dims_fn or (lambda p: _default_prober(p, ffprobe_bin))

    out = {}
    for p in image_paths or []:
        if not p:
            continue
        try:
            stats = stats_fn(p)
        except Exception:
            stats = None
        try:
            dims = dims_fn(p)
        except Exception:
            dims = None
        width = dims[0] if dims else None
        height = dims[1] if dims else None
        reason = None
        if is_monochrome_or_gradient(stats, min_distinct=min_distinct, solid_top1_threshold=solid_top1,
                                     solid_max_distinct=solid_max_distinct):
            reason = "monochrome_or_gradient"
        elif width is not None and height is not None and is_extreme_aspect(width, height, max_ratio=aspect_max):
            reason = "extreme_aspect"
        out[str(p)] = {
            "excluded": reason is not None,
            "reason": reason,
            "stats": stats,
            "width": width,
            "height": height,
        }
    return out


# ---------------------------------------------------------------------------
# 商品画像の分類（vision）
# ---------------------------------------------------------------------------

# 分類カテゴリ: product_solo（商品単体の物撮り）/ product_in_use（使用シーン）/
# logo_banner（ロゴ・バナー・広告テキスト画像）/ unrelated（無関係）
_ADOPT_CATEGORIES = ("product_solo", "product_in_use")
_NON_ADOPT_CATEGORIES = ("logo_banner", "unrelated")
_VALID_CATEGORIES = _ADOPT_CATEGORIES + _NON_ADOPT_CATEGORIES

_CLASSIFY_PROMPT = (
    "以下の画像は商品LPから取得された素材候補です（順序どおり image1..imageN）。"
    "各画像を次のいずれかに分類してください。"
    "category は必ず ['product_solo','product_in_use','logo_banner','unrelated'] のいずれか。"
    " product_solo=商品パッケージ単体の物撮り、product_in_use=商品が実際に使われている/"
    "手に持たれている/シーンに置かれている、logo_banner=ロゴ・バナー・広告テキスト・"
    "before/after 比較画像・成分表・受賞歴の様な図版、unrelated=商品と無関係。"
    "★重要: 商品そのものが写っていない画像——純粋な装飾背景・単色やグラデーションの色面・"
    "模様やテクスチャだけ・被写体不在の背景板——は必ず unrelated に分類すること"
    "（商品LPの飾り背景を商品と誤認しないこと）。"
    "sharpness は 'high'（ボケが少ない）/'low'（ボケ・低画質）の2択。"
    "dominant_colors は画面を占める代表色2〜3件を '#RRGGBB' 形式のリストで（無理なら空配列）。"
    "見えるものだけで判定し、推測で商品を補わないこと。"
    "出力はJSON配列のみ。要素は {index:int(1始まり), category:str, sharpness:str, "
    "dominant_colors:[str,...]} だけを含めること。コードフェンス禁止。"
)


def _normalize_hex(color):
    if not isinstance(color, str):
        return None
    c = color.strip()
    if not c:
        return None
    if not c.startswith("#"):
        c = "#" + c
    if len(c) != 7:
        return None
    for ch in c[1:]:
        if ch not in "0123456789abcdefABCDEF":
            return None
    return "#" + c[1:].lower()


_VISION_DEFAULT = object()  # 「引数未指定＝デフォルトを解決」を明示するためのセンチネル


def classify_product_images(image_paths, vision_call=_VISION_DEFAULT, timeout_sec=600,
                            prefilter_enabled=True, ffmpeg_bin=None,
                            color_stats_fn=None, dims_fn=None, cfg=None):
    """画像リストを（決定論フィルタ→vision）で分類し、各画像の分類 dict を返す。

    ★2026-07-25 追加（診断 P1-5）: vision の前段に決定論フィルタ（deterministic_prefilter）を
    噛ませ、単色/グラデ装飾背景（例: LPの純ピンク背景 001.png）・極端アスペクト（バナー形状）を
    vision へ回す前に確実に落とす。除外された画像は adopted=False・category="unrelated"・
    reason="monochrome_or_gradient"|"extreme_aspect" とし、vision コストも消費しない。

    Returns: list[dict] — 入力 image_paths と 1:1・同順対応。各要素は
        {"path": str, "category": str, "sharpness": str, "dominant_colors": [hex,...],
         "adopted": bool, "reason": str|None}
        - adopted=True: category が product_solo / product_in_use かつ sharpness=high
        - adopted=False: それ以外（reason に不採用理由を短く記載）
        vision呼び出しに失敗した場合は縮退モード=全採用（adopted=True・reason="vision_failed"）
        で返し、警告メッセージは呼び出し側の warnings に追記される想定（本関数は例外を送出
        しない）。

    vision_call: 差し込み可能な call_claude_vision_json 互換関数。テストで注入する
        （既定は claude_runner.call_claude_vision_json）。
    prefilter_enabled / color_stats_fn / dims_fn / ffmpeg_bin / cfg: 決定論フィルタの制御・DI。
    """
    paths = [str(p) for p in (image_paths or []) if p]
    if not paths:
        return []

    # --- 前段: 決定論フィルタ（単色/グラデ背景・極端アスペクトを機械除外） ---
    prefilter_map = {}
    if prefilter_enabled:
        try:
            prefilter_map = deterministic_prefilter(
                paths, ffmpeg_bin=ffmpeg_bin, color_stats_fn=color_stats_fn,
                dims_fn=dims_fn, cfg=cfg,
            )
        except Exception:
            prefilter_map = {}
    excluded_entries = {}
    vision_paths = []
    for p in paths:
        info = prefilter_map.get(p)
        if info and info.get("excluded"):
            excluded_entries[p] = {
                "path": p, "category": "unrelated", "sharpness": "",
                "dominant_colors": [], "adopted": False,
                "reason": info.get("reason") or "prefiltered",
            }
        else:
            vision_paths.append(p)

    # 全て前段除外なら vision を呼ばずに返す（コスト節約）。
    if not vision_paths:
        return [excluded_entries[p] for p in paths]

    vision_results = _classify_via_vision(vision_paths, vision_call=vision_call, timeout_sec=timeout_sec)
    vision_by_path = {r["path"]: r for r in vision_results}

    # 入力順に、前段除外分と vision 分類分をマージして返す。
    merged = []
    for p in paths:
        if p in excluded_entries:
            merged.append(excluded_entries[p])
        else:
            merged.append(vision_by_path.get(p) or {
                "path": p, "category": "product_solo", "sharpness": "high",
                "dominant_colors": [], "adopted": True, "reason": "vision_missing",
            })
    return merged


def _classify_via_vision(image_paths, vision_call=_VISION_DEFAULT, timeout_sec=600):
    """画像リストを vision に投げ、各画像の分類 dict を返す（前段フィルタ通過後の本体）。

    vision_call: 差し込み可能な call_claude_vision_json 互換関数。テストで注入する
        （既定は claude_runner.call_claude_vision_json）。
    """
    paths = [str(p) for p in (image_paths or []) if p]
    if not paths:
        return []

    # 既定vision呼び出しは遅延importして循環依存を避ける（claude_runner→configの経路）。
    # 明示的に vision_call=None を渡された場合は「vision不能」として縮退させ、
    # 引数を渡さなかった場合（sentinel）だけ既定のCLIを解決する。
    if vision_call is _VISION_DEFAULT:
        try:
            from pipeline.claude_runner import call_claude_vision_json as _default_call
        except Exception:
            _default_call = None
        vision_call = _default_call

    fallback_all_adopted = [
        {
            "path": p, "category": "product_solo", "sharpness": "high",
            "dominant_colors": [], "adopted": True, "reason": "vision_unavailable",
        }
        for p in paths
    ]

    if vision_call is None:
        return fallback_all_adopted

    # 1バッチ最大6枚（プロンプト内のindex参照が崩れないように分割）。
    _MAX_BATCH = 6
    results = [None] * len(paths)
    for start in range(0, len(paths), _MAX_BATCH):
        batch = paths[start:start + _MAX_BATCH]
        prompt = _CLASSIFY_PROMPT + "\n\n" + "\n".join(
            "image{}: {}".format(i + 1, p) for i, p in enumerate(batch)
        )
        try:
            resp = vision_call(prompt, batch, timeout_sec=timeout_sec)
        except Exception as exc:
            resp = {"ok": False, "error": "exception: {}".format(exc)}
        if not resp or not resp.get("ok"):
            # このバッチは失敗 → 縮退（このバッチ分は全採用）
            for i, p in enumerate(batch):
                results[start + i] = {
                    "path": p, "category": "product_solo", "sharpness": "high",
                    "dominant_colors": [], "adopted": True, "reason": "vision_failed",
                }
            continue
        data = resp.get("data") or []
        by_index = {}
        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue
                try:
                    idx = int(item.get("index"))
                except (TypeError, ValueError):
                    continue
                by_index[idx] = item
        for i, p in enumerate(batch):
            item = by_index.get(i + 1)
            if item is None:
                # 応答漏れは縮退で採用（見落としで捨てるより保守的）
                results[start + i] = {
                    "path": p, "category": "product_solo", "sharpness": "high",
                    "dominant_colors": [], "adopted": True, "reason": "vision_missing_index",
                }
                continue
            cat = item.get("category")
            if cat not in _VALID_CATEGORIES:
                cat = "unrelated"
            sharp = item.get("sharpness")
            if sharp not in ("high", "low"):
                sharp = "low"
            colors_in = item.get("dominant_colors") or []
            colors = []
            if isinstance(colors_in, list):
                for c in colors_in:
                    hx = _normalize_hex(c)
                    if hx and hx not in colors:
                        colors.append(hx)
                    if len(colors) >= 3:
                        break
            adopted = cat in _ADOPT_CATEGORIES and sharp == "high"
            reason = None if adopted else ("low_sharpness" if sharp != "high" else cat)
            results[start + i] = {
                "path": p, "category": cat, "sharpness": sharp,
                "dominant_colors": colors, "adopted": adopted, "reason": reason,
            }

    # 念のため None が残っていれば縮退で埋める
    for i, p in enumerate(paths):
        if results[i] is None:
            results[i] = {
                "path": p, "category": "product_solo", "sharpness": "high",
                "dominant_colors": [], "adopted": True, "reason": "vision_missing",
            }
    return results


def save_product_manifest(product_dir, entries, warnings=None):
    """classify_product_images() の結果を product_dir/product_manifest.json に書き出す。

    書き込み失敗は握りつぶし、警告メッセージのみを (warnings に append できる) 文字列で返す。
    Returns: list[str] warnings（呼び出し側の warnings.extend() 用）。
    """
    warn_out = list(warnings or [])
    try:
        os.makedirs(str(product_dir), exist_ok=True)
        path = os.path.join(str(product_dir), "product_manifest.json")
        payload = {
            "version": 1,
            "entries": [dict(e) for e in (entries or [])],
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception as exc:
        warn_out.append("product_manifest.json の保存に失敗しました: {}".format(exc))
    return warn_out


# ---------------------------------------------------------------------------
# 参考spec → 商品shot判定
# ---------------------------------------------------------------------------

# 「物撮り shot」検出用の英語キーワード（visual_desc_en 内で単語境界一致）。
_PRODUCT_SHOT_KEYWORDS = (
    "product", "bottle", "package", "packaging", "jar", "tube", "box",
    "container", "cosmetic", "cosmetics", "serum", "cream", "lotion",
    "close-up of", "close up of",
)


def _shot_is_product_shot(shot, shots_ref_by_time=None):
    """shot（skeleton 由来。reference_visual を含む可能性あり）が「物撮り shot」か判定する。

    判定基準（いずれか成立で True）:
      1. shot.reference_visual.desc_en に商品キーワードが含まれる
      2. shots_ref_by_time が渡され、対応する shots_ref item が
         has_product_logo=True または visual_desc_en に商品キーワードを含む
    """
    if not isinstance(shot, dict):
        return False
    rv = shot.get("reference_visual") if isinstance(shot.get("reference_visual"), dict) else None
    if rv:
        desc = (rv.get("desc_en") or "").lower()
        for kw in _PRODUCT_SHOT_KEYWORDS:
            if kw in desc:
                return True
    # shots_ref_by_time が渡されていれば追加検査（has_product_logo 等）
    if shots_ref_by_time and isinstance(shots_ref_by_time, list):
        for sr in shots_ref_by_time:
            if not isinstance(sr, dict):
                continue
            if sr.get("has_product_logo"):
                return True
            desc = (sr.get("visual_desc_en") or "").lower()
            for kw in _PRODUCT_SHOT_KEYWORDS:
                if kw in desc:
                    return True
    return False


def product_shot_indices(shots, reference_spec=None):
    """shots のうち「商品を主役に据えてよい shot」のインデックス配列を返す（純関数）。

    判定は「そのshot固有の根拠」のみで行う（他shotの情報を混ぜない）:
      - まず shot.reference_visual.desc_en を見る（skeleton由来。1shotに集約済み）。
      - reference_visual が欠落・空 desc_en のshotは、対応する shots_ref を「位置ベースで
        1:1 に対応付けた」ときだけ参照する。1:1に対応付かない場合は判定不能として False。
        （codex-review P1 指摘: shots_ref 全体を無差別に見ると1件の product_logo だけで
        無関係shotまで商品shot扱いになり、割当限定の意図が壊れる。）

    Args:
        shots: skeleton/plan shot dict の list。
        reference_spec: 参考 spec dict。shots_ref を持つ。任意（無ければ shot 側のみ）。

    Returns: sorted list[int]（0-indexed）
    """
    if not shots:
        return []
    shots_ref = None
    if isinstance(reference_spec, dict):
        shots_ref = reference_spec.get("shots_ref") or []
    # skeleton の shot 数と shots_ref の要素数が一致するときだけ、位置ベースで 1:1
    # 対応付ける（skeleton は shots_ref からカット境界を組み立てるため、境界マージ・
    # 分割が発生しない限り一致する。一致しないときはあくまで shot 側の情報だけを見る）。
    can_align = bool(shots_ref) and len(shots_ref) == len(shots)
    indices = []
    for i, sh in enumerate(shots):
        rv = sh.get("reference_visual") if isinstance(sh, dict) else None
        # 1) shot 側の reference_visual.desc_en が確定していればそれを最優先。
        if rv and (rv.get("desc_en") or "").strip():
            if _shot_is_product_shot(sh):
                indices.append(i)
                continue
            # desc_en があってキーワード不一致なら商品shot扱いしない（他情報で救わない）。
            continue
        # 2) reference_visual が無いときのみ、位置対応の shots_ref[i] を1件だけ検査する。
        if can_align:
            sr = shots_ref[i]
            aligned = [sr] if isinstance(sr, dict) else None
            if aligned and _shot_is_product_shot(sh, shots_ref_by_time=aligned):
                indices.append(i)
        # 位置対応が取れないshotは判定不能。conservative に False で扱う
        # （下流の縮退＝フック+CTAで安全側にフォールバックする）。
    return indices


# ---------------------------------------------------------------------------
# ショットへの決定論的割り当て
# ---------------------------------------------------------------------------

def _assign_legacy(shots, image_paths):
    """従来の割当ロジック（フック+CTA+中間均等）。reference_spec=None の後方互換用。"""
    result = [dict(s) for s in shots]
    paths = [p for p in (image_paths or []) if p]
    n = len(result)

    if n == 0 or not paths:
        return result

    if n == 1:
        result[0]["image_path"] = paths[0]
        return result

    hook_img = paths[0]
    cta_img = paths[1] if len(paths) >= 2 else paths[0]
    result[0]["image_path"] = hook_img
    result[n - 1]["image_path"] = cta_img

    remaining = paths[2:] if len(paths) >= 2 else []
    middle_indices = list(range(1, n - 1))

    if middle_indices and remaining:
        count_m = len(middle_indices)
        count_r = len(remaining)
        if count_r >= count_m:
            for j, mi in enumerate(middle_indices):
                img_idx = (j * count_r) // count_m
                result[mi]["image_path"] = remaining[img_idx]
        else:
            for k in range(count_r):
                pos = (k * count_m) // count_r
                mi = middle_indices[pos]
                result[mi]["image_path"] = remaining[k]

    return result


def assign_images_to_shots(shots, image_paths, reference_spec=None):
    """shots(ショットリスト)へimage_pathsを決定論的に割り当てる（非破壊）。

    ★2026-07-25 変更（設計原則）:
        参考動画のスケルトンが唯一の被写体・構図の根拠であり、商品画像は「参考shotの
        被写体スロットへの差し替え素材」でしかない。従って image-to-video の起点
        （--start-image / mock の -loop -i）として商品画像を割当てるのは、参考spec上で
        「物撮り shot」と判定できる shot に限定する。それ以外の shot は image_path を
        載せない = テキストto動画（gradients / higgsfield 通常経路）で参考の絵を作らせる。

    Args:
        shots: skeleton 由来の shot dict list。reference_visual.desc_en があると精度が高い。
        image_paths: 商品画像のローカル絶対パス配列。
        reference_spec: 参考動画 v2 spec（dict）。None のときは従来経路（フック+CTA+中間）で
            割当てる（後方互換。旧テスト・reference無しモード用）。

    ロジック（reference_spec 有り）:
        - 商品shot（product_shot_indices）を抽出。
        - 商品shotが0件: フック(shots[0])+CTA(shots[-1]) の2点のみに割当（従来の縮退）。
        - 商品shotが1件以上: そのshotにだけ image_path を均等に配分する（画像が枯渇したら
          残りは image_path を載せない）。フック/CTAが商品shotに含まれていればそこにも入る。
        - shots_ref から商品shotが検出できないときも、フック+CTAには image_path を1枚ずつ
          載せる（これは「参考の hook_end/cta_start は物撮り率が高いため許容」の縮退動作）。

    ロジック（reference_spec=None・後方互換）:
        従来の _assign_legacy() で処理（フック+CTA+中間均等）。
    """
    shots = shots or []
    paths = [p for p in (image_paths or []) if p]
    n = len(shots)

    if reference_spec is None:
        return _assign_legacy(shots, paths)

    result = [dict(s) for s in shots]
    if n == 0 or not paths:
        return result

    product_indices = product_shot_indices(shots, reference_spec=reference_spec)

    if not product_indices:
        # 縮退: フック(0) と CTA(n-1) の2点のみ
        if n == 1:
            result[0]["image_path"] = paths[0]
        else:
            result[0]["image_path"] = paths[0]
            result[n - 1]["image_path"] = paths[1] if len(paths) >= 2 else paths[0]
        return result

    # 商品shotのみに image_path を配分（枯渇したら image_path キー無し）
    for j, idx in enumerate(product_indices):
        if j < len(paths):
            result[idx]["image_path"] = paths[j]
    return result

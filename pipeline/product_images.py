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

def _is_safe_url(url):
    """http/https以外、または私有系(loopback/private/link-local等)ホストなら False。

    DNS解決は行わない（ホスト名文字列/IPリテラルのみの判定。実ネットワーク禁止
    環境でも決定論的に判定できるようにするため）。
    """
    if not url or not isinstance(url, str):
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
        try:
            resolved = urljoin(base_url, raw.strip())
        except Exception:
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
# ショットへの決定論的割り当て
# ---------------------------------------------------------------------------

def assign_images_to_shots(shots, image_paths):
    """shots(ショットリスト)へimage_pathsを決定論的に割り当てる（非破壊）。

    - shots[0](フック)には image_paths[0]。
    - 最終shot(CTA)には image_paths[1](2枚以上ある場合) / image_paths[0](1枚のみの場合)。
    - 残りの画像は中間shot(先頭・末尾を除く)へ等間隔に配分する。画像が尽きたら
      それ以降(間に合わなかった)中間shotには "image_path" キーを付けない。
    - shot dictはコピーして返す（引数のshotsは変更しない）。

    Returns: 新しいshotリスト（各要素は元shot dictの浅いコピー + 割当時は"image_path"追加）。
    """
    shots = shots or []
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
            # 中間shot全てに1枚ずつ、remainingを間引いて均等割り当て
            for j, mi in enumerate(middle_indices):
                img_idx = (j * count_r) // count_m
                result[mi]["image_path"] = remaining[img_idx]
        else:
            # remainingがmiddle shotより少ない: 均等な間隔でcount_r個のshotにのみ割り当て、
            # それ以外の中間shotは "image_path" キー無し（画像が尽きた扱い）。
            for k in range(count_r):
                pos = (k * count_m) // count_r
                mi = middle_indices[pos]
                result[mi]["image_path"] = remaining[k]

    return result

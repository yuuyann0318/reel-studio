# -*- coding: utf-8 -*-
"""font_class(gothic/mincho/rounded/pop/handwriting) -> 実在する日本語フォント名の対応表。

参考テロップの「フォントの雰囲気」を ASS(\\fn) と Premiere に忠実再現するための土台。
このプロジェクトは render.py が `subtitles=...:fontsdir=<assets/fonts>` で libass に
フォントを供給する（assets/fonts 同梱フォントのみを参照）。よって font_class は
**assets/fonts に実在し、かつ libass が \\fn で実際にマッチできると実測で確認した**
ファミリ名にのみ写像する（捏造・存在しないフォント名は返さない）。

実測(2026-07-26, 同梱 bin/ffmpeg + libass, fontsdir=assets/fonts, 540x960 レンダの md5 差分で確認):
  - "Noto Sans JP Black"     (NotoSansJP-Black.ttf)     -> 固有描画 ✓  gothic
  - "Noto Serif JP Black"    (NotoSerifJP-Black.ttf)    -> 固有描画 ✓  mincho
  - "Zen Maru Gothic Black"  (ZenMaruGothic-Black.ttf)  -> 固有描画 ✓  rounded
  - "Mochiy Pop One"         (MochiyPopOne-Regular.ttf) -> 固有描画 ✓  pop
  - "Klee One SemiBold"      (KleeOne-SemiBold.ttf)     -> 固有描画 ✓  handwriting
  - "Yusei Magic"            (YuseiMagic-Regular.ttf)   -> 固有描画 ✓  handwriting(代替)
  ※ "M PLUS Rounded 1c Black" (MPLUSRounded1c-Black.ttf) は \\fn 名がフォールバックと
     同一md5になりマッチしなかった(libass が拾えない)ため rounded には採用しない。

同梱フォントは Black/SemiBold の単一ウェイトが中心のため、weight(normal/bold/heavy)は
主に ASS の \\b フラグ（合成ボールド）で補助する。真の多段ウェイトは持たないので、
weight 忠実度は粗い（この制約は STYLE_SPEC.md にも明記）。

Python 3.9 互換構文のみ。stdlib のみ。
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

try:
    from pipeline.config import project_root
except Exception:  # pragma: no cover - 単体 import 時の保険
    project_root = None  # type: ignore


# font_class の許容語彙（vision/spec 側と一致させる）。
FONT_CLASSES = ("gothic", "mincho", "rounded", "pop", "handwriting")
DEFAULT_FONT_CLASS = "gothic"

# weight の許容語彙。
FONT_WEIGHTS = ("normal", "bold", "heavy")
DEFAULT_FONT_WEIGHT = "bold"

# font_class -> (libass ファミリ名, 同梱ファイル名)。
# ファイルは assets/fonts/ に実在するもののみ。実在確認は resolve/enumerate で os.path.exists。
_FONT_CLASS_TABLE: Dict[str, Dict[str, str]] = {
    "gothic": {"family": "Noto Sans JP Black", "file": "NotoSansJP-Black.ttf"},
    "mincho": {"family": "Noto Serif JP Black", "file": "NotoSerifJP-Black.ttf"},
    "rounded": {"family": "Zen Maru Gothic Black", "file": "ZenMaruGothic-Black.ttf"},
    "pop": {"family": "Mochiy Pop One", "file": "MochiyPopOne-Regular.ttf"},
    "handwriting": {"family": "Klee One SemiBold", "file": "KleeOne-SemiBold.ttf"},
}

# 既定フォント（fallback 先）。gothic は必ず存在する前提だが、無い場合はさらに素の
# "Noto Sans JP Black" 名を返す（libass 既定にフォールバックしても描画は継続する）。
_HARD_DEFAULT_FAMILY = "Noto Sans JP Black"


def fonts_dir() -> str:
    """同梱フォントディレクトリの絶対パス（assets/fonts）。"""
    if project_root is not None:
        try:
            return str(project_root() / "assets" / "fonts")
        except Exception:
            pass
    # project_root が使えない場合はこのファイルからの相対で解決。
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(os.path.dirname(here), "assets", "fonts")


def _file_exists(file_name: str) -> bool:
    if not file_name:
        return False
    return os.path.exists(os.path.join(fonts_dir(), file_name))


def available_jp_fonts() -> Dict[str, Dict[str, str]]:
    """assets/fonts に実在するものだけに絞った font_class -> {family,file,exists} を返す。

    実機のファイル存在で裏取りした結果のみ。存在しない font_class は 'exists': False で
    残すが family は fallback 用途に据え置く（呼び出し側は exists を見て warning を出す）。
    """
    out: Dict[str, Dict[str, str]] = {}
    for cls, info in _FONT_CLASS_TABLE.items():
        exists = _file_exists(info["file"])
        out[cls] = {"family": info["family"], "file": info["file"], "exists": exists}
    return out


def normalize_font_class(value: Any) -> str:
    """font_class 語を許容語彙へ正規化する。unknown/空は DEFAULT_FONT_CLASS。"""
    s = (str(value).strip().lower() if value is not None else "")
    if s in FONT_CLASSES:
        return s
    # よくある表記ゆれの吸収。
    alias = {
        "sans": "gothic", "sans_serif": "gothic", "gothic_bold": "gothic",
        "serif": "mincho", "min": "mincho", "mincyo": "mincho",
        "round": "rounded", "maru": "rounded", "maru_gothic": "rounded",
        "casual": "pop", "comic": "pop",
        "hand": "handwriting", "brush": "handwriting", "script": "handwriting",
    }
    return alias.get(s, DEFAULT_FONT_CLASS)


def normalize_weight(value: Any) -> str:
    s = (str(value).strip().lower() if value is not None else "")
    if s in FONT_WEIGHTS:
        return s
    alias = {"regular": "normal", "medium": "normal", "black": "heavy",
             "extrabold": "heavy", "semibold": "bold", "demibold": "bold", "": DEFAULT_FONT_WEIGHT}
    return alias.get(s, DEFAULT_FONT_WEIGHT)


def weight_to_bold_flag(weight: Any) -> int:
    """weight -> ASS Bold フラグ(-1=bold / 0=normal)。

    同梱フォントは単一ウェイトのため \\b は合成ボールドの補助にしかならないが、
    bold/heavy を normal から区別する意図は反映する。
    """
    w = normalize_weight(weight)
    return -1 if w in ("bold", "heavy") else 0


def resolve_font(font_class: Any, weight: Any = None) -> Tuple[str, int, List[str]]:
    """font_class(+weight) を (libass ファミリ名, bold_flag, warnings) に解決する。

    - 対応 font_class のファイルが assets/fonts に実在すればそのファミリ名を返す。
    - 実在しなければ gothic(既定) にフォールバックし warning を積む。gothic も無ければ
      ハード既定名を返す（libass 既定に委ねても描画は止めない）。

    Returns:
      (family_name: str, bold_flag: int(-1/0), warnings: list[str])
    """
    warnings: List[str] = []
    cls = normalize_font_class(font_class)
    info = _FONT_CLASS_TABLE.get(cls)
    if info is None:  # 到達しない想定（normalize が保証）だが保険
        info = _FONT_CLASS_TABLE[DEFAULT_FONT_CLASS]
        cls = DEFAULT_FONT_CLASS
    if not _file_exists(info["file"]):
        # 要求クラスのフォントが無い → gothic 代替 + warning。
        warnings.append(
            "font_class={} のフォント({})が assets/fonts に見つからないため gothic 代替".format(cls, info["file"])
        )
        gothic = _FONT_CLASS_TABLE[DEFAULT_FONT_CLASS]
        if _file_exists(gothic["file"]):
            return gothic["family"], weight_to_bold_flag(weight), warnings
        warnings.append("gothic フォント({})も見つからないため libass 既定に委ねる".format(gothic["file"]))
        return _HARD_DEFAULT_FAMILY, weight_to_bold_flag(weight), warnings
    return info["family"], weight_to_bold_flag(weight), warnings


def resolve_font_family(font_class: Any, weight: Any = None) -> str:
    """resolve_font のファミリ名だけが欲しい薄いヘルパ。"""
    family, _bold, _w = resolve_font(font_class, weight)
    return family

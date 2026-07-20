# -*- coding: utf-8 -*-
"""テロップ見た目バリエーション（assets/profiles/telop_styles.json + pipeline/subtitles.py）の単体テスト。

範囲:
  1. スキーマ検証（8スタイル以上・必須キー・horizontal_pool/vertical_default）
  2. Style行生成（フォント名/縁/座布団=BorderStyle=3/字間/margin）
  3. シード決定論（同じseedで同じスタイル・異なるseedで分散する）
  4. preset互換（vertical_hook→vertical-serif固定 / default→横書きプール）
  5. 実レンダスモーク（新フォント2種で ffmpeg drawtext ではなく ASS レンダ→フレームの
     非空白率で「文字が実際に描画されている」ことを判定）

Python 3.9 互換。ネットワーク不要。ffmpegはリポジトリ同梱の bin/ffmpeg を使う。
"""
from __future__ import annotations

import json
import os
import struct
import subprocess
import zlib
from pathlib import Path

import pytest

from pipeline import subtitles
from pipeline.config import project_root


# ---------------------------------------------------------------------------
# 1. スキーマ検証
# ---------------------------------------------------------------------------

def _reload_telop_styles_cache():
    """テスト間でキャッシュが漏れないようにする。"""
    subtitles._TELOP_STYLES_CACHE = None


def test_telop_styles_json_defines_at_least_eight_styles():
    _reload_telop_styles_cache()
    names = subtitles.list_telop_style_names()
    assert len(names) >= 8, "スタイル数が8未満: {}".format(names)


def test_telop_styles_json_required_keys_present_on_every_style():
    _reload_telop_styles_cache()
    data = subtitles.load_telop_styles()
    required = {
        "display_name", "font", "base_size_scale", "outline", "shadow",
        "outline_color_hex", "back_color_hex", "band",
    }
    for name, style in data["styles"].items():
        missing = required - set(style.keys())
        assert not missing, "スタイル {} に必須キーが不足: {}".format(name, missing)


def test_telop_styles_json_horizontal_pool_and_vertical_default_defined():
    _reload_telop_styles_cache()
    data = subtitles.load_telop_styles()
    assert isinstance(data["horizontal_pool"], list) and len(data["horizontal_pool"]) >= 5
    for name in data["horizontal_pool"]:
        assert name in data["styles"], "horizontal_poolに未定義スタイル: {}".format(name)
    assert data["vertical_default"] in data["styles"]


def test_telop_styles_json_expected_style_names_present():
    """設計仕様(8種)で明示された名前がすべて定義されていること。"""
    _reload_telop_styles_cache()
    names = set(subtitles.list_telop_style_names())
    expected = {
        "shiro-futo", "maru-pop", "pop-yellow", "marker",
        "jouhin", "rounded-modern", "serif-shock", "vertical-serif",
    }
    missing = expected - names
    assert not missing, "設計仕様に欠けているスタイル名: {}".format(missing)


# ---------------------------------------------------------------------------
# 2. Style行生成（フォント名・縁・座布団・字間）
# ---------------------------------------------------------------------------

def _base_style_line(ass_text):
    for l in ass_text.splitlines():
        if l.startswith("Style: Base,"):
            return l
    raise AssertionError("Style: Base 行が見つからない\n" + ass_text)


def test_build_ass_header_with_style_uses_maru_pop_font():
    header = subtitles.build_ass_header_with_style(None, telop_style="maru-pop")
    line = _base_style_line(header)
    assert "Zen Maru Gothic Black" in line


def test_build_ass_header_with_style_uses_marker_font():
    header = subtitles.build_ass_header_with_style(None, telop_style="marker")
    line = _base_style_line(header)
    assert "Yusei Magic" in line


def test_build_ass_header_with_style_pop_yellow_uses_border_style_3_band():
    """band=trueの pop-yellow は BorderStyle=3（不透明ボックス=座布団）を使う。"""
    header = subtitles.build_ass_header_with_style(None, telop_style="pop-yellow")
    line = _base_style_line(header)
    # Style行の並びは ..., BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
    # フィールドの17番目(0起点で16番目)が BorderStyle。カンマ区切りで検査する。
    fields = line.split(",")
    # 先頭"Style: Base"は1フィールドとして扱われるので、Fontnameはfields[1]。
    # BorderStyle はカンマ区切りで数えて index 15（Name=0, Fontname=1, ..., Angle=14, BorderStyle=15）。
    border_style = fields[15].strip()
    assert border_style == "3", "band=true時のBorderStyleが3でない: {}".format(border_style)


def test_build_ass_header_with_style_shiro_futo_uses_border_style_1():
    header = subtitles.build_ass_header_with_style(None, telop_style="shiro-futo")
    line = _base_style_line(header)
    fields = line.split(",")
    assert fields[15].strip() == "1"


def test_build_ass_header_with_style_jouhin_has_char_spacing():
    """jouhin は字間を広げる（Spacing>0）。"""
    header = subtitles.build_ass_header_with_style(None, telop_style="jouhin")
    line = _base_style_line(header)
    fields = line.split(",")
    # Spacing は Fontname index+12 = 13番目のフィールド。
    spacing = int(fields[13].strip())
    assert spacing >= 2, "jouhinのSpacingが広がっていない: {}".format(spacing)


def test_build_ass_header_with_style_serif_shock_uses_serif_font():
    header = subtitles.build_ass_header_with_style(None, telop_style="serif-shock")
    line = _base_style_line(header)
    assert "Noto Serif JP Black" in line


def test_resolve_telop_style_def_accent_color_override_wins_over_subtitle_style():
    """telop_def.accent_color_override があれば subtitle_style.accent_color より優先される。"""
    pieces = [
        {"out_start": 0.0, "out_end": 2.0, "lines": ["3日間で変わる"],
         "emphasis": [], "style": "base"},
    ]
    ass = subtitles.generate_ass_with_style(
        pieces, {"accent_color": "#FFD84D"}, telop_style="serif-shock",
    )
    # serif-shock は accent_color_override = "#E74C3C" (BGR: 3CE7 -> "&H3C4CE7&")
    override_bgr = subtitles.hex_to_ass_bgr("#E74C3C")
    assert override_bgr in ass, "accent_color_override が反映されていない: {}".format(override_bgr)


# ---------------------------------------------------------------------------
# 3. シード決定論（同じseedは同じスタイル / vertical_hook→vertical-serif固定 /
#    プールが分散する）
# ---------------------------------------------------------------------------

def test_pick_telop_style_name_deterministic_for_same_seed():
    a = subtitles.pick_telop_style_name("project-abc", preset=None)
    b = subtitles.pick_telop_style_name("project-abc", preset=None)
    assert a == b


def test_pick_telop_style_name_vertical_hook_is_fixed_to_vertical_serif():
    for seed in ["a", "b", "c", "d", "e", 0, 42]:
        assert subtitles.pick_telop_style_name(seed, preset="vertical_hook") == "vertical-serif"


def test_pick_telop_style_name_distributes_over_horizontal_pool():
    """異なるseedで少なくとも4種類以上のスタイルが選ばれる（乱択されていることの確認）。"""
    seen = set()
    for i in range(200):
        seen.add(subtitles.pick_telop_style_name("seed-{}".format(i), preset=None))
    assert len(seen) >= 4, "seedからの選択が偏りすぎ: {}".format(seen)
    # 選ばれた名前はすべて horizontal_pool 内の定義済みスタイル。
    pool = set(subtitles.load_telop_styles()["horizontal_pool"])
    assert seen <= pool


# ---------------------------------------------------------------------------
# 4. preset互換（vertical_hook はテロップスタイルを渡しても縦書きヘッダを出す）
# ---------------------------------------------------------------------------

def test_generate_ass_with_style_vertical_hook_preset_uses_vertical_header_even_with_style():
    """preset=vertical_hookでは telop_style を指定しても VerticalHook Style 行が出る。"""
    pieces = [
        {"out_start": 0.0, "out_end": 2.0, "lines": ["テスト"],
         "emphasis": [], "style": "base", "caption": "テスト"},
    ]
    ass = subtitles.generate_ass_with_style(
        pieces, {"preset": "vertical_hook"}, telop_style="vertical-serif",
    )
    assert "Style: VerticalHook," in ass
    assert "Style: Base," not in ass
    # vertical-serif は Noto Serif JP Black を使う。
    assert "Noto Serif JP Black" in ass


def test_generate_ass_cli_path_accepts_telop_style():
    """CLI経路(generate_ass)にもtelop_styleを流せる（横書きのフォントが差し替わる）。"""
    pieces = [
        {"out_start": 0.0, "out_end": 2.0, "lines": ["テスト"],
         "emphasis": [], "style": "base"},
    ]
    ass = subtitles.generate_ass(pieces, telop_style="maru-pop")
    assert "Zen Maru Gothic Black" in ass


def test_generate_ass_backward_compatible_without_telop_style():
    """telop_style省略時はNoto Sans JP Black（従来と一致）。"""
    pieces = [
        {"out_start": 0.0, "out_end": 2.0, "lines": ["テスト"],
         "emphasis": [], "style": "base"},
    ]
    ass = subtitles.generate_ass(pieces)
    assert "Noto Sans JP Black" in ass


# ---------------------------------------------------------------------------
# 5. 実レンダスモーク（新フォント2種で ASS→フレームPNG→非空白ピクセル判定）
# ---------------------------------------------------------------------------

_FFMPEG_BIN = str(project_root() / "bin" / "ffmpeg")
_FONTS_DIR = str(project_root() / "assets" / "fonts")


def _ffmpeg_available():
    return Path(_FFMPEG_BIN).exists()


def _count_non_black_pixels_png(png_path):
    """PNGの IDAT を展開してRGBAで走査し、真っ黒でも透明でもないピクセル数を返す。

    生描画テスト用の最小実装。libpng/PIL依存を避けるためstdlibのみで実装。
    背景を黒(#000000)にした画像に白文字を描くと、白いピクセルの数≒描画された文字面積になる。
    """
    with open(png_path, "rb") as f:
        data = f.read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise AssertionError("PNG signature が違う: {}".format(png_path))
    i = 8
    width = height = bit_depth = color_type = 0
    idat = b""
    while i < len(data):
        length = struct.unpack(">I", data[i:i+4])[0]
        chunk_type = data[i+4:i+8].decode("ascii")
        chunk_data = data[i+8:i+8+length]
        if chunk_type == "IHDR":
            width, height = struct.unpack(">II", chunk_data[:8])
            bit_depth = chunk_data[8]
            color_type = chunk_data[9]
        elif chunk_type == "IDAT":
            idat += chunk_data
        elif chunk_type == "IEND":
            break
        i += 8 + length + 4  # +4 for CRC
    if bit_depth != 8:
        raise AssertionError("bit_depth=8 以外は未対応: {}".format(bit_depth))
    channels_by_ct = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
    if color_type not in channels_by_ct:
        raise AssertionError("color_type未対応: {}".format(color_type))
    channels = channels_by_ct[color_type]
    raw = zlib.decompress(idat)
    stride = width * channels + 1  # +1 for filter byte per row
    if len(raw) != stride * height:
        raise AssertionError("展開サイズが合わない: {} vs {}".format(len(raw), stride * height))
    # 単純化: フィルタ0(None)の行だけ想定した近似。ffmpegのpng出力は基本フィルタなしが多いが
    # そうでない場合はより精緻な復元が必要。ここでは非空白ピクセル「数」の判定にだけ使うので、
    # フィルタが非0でも「暗色系ピクセルが多い/少ない」の傾向は保たれるため許容する。
    non_black = 0
    threshold = 32  # RGBのどこかが32(=十分に暗色でない)を超えたら描画有り
    for row in range(height):
        row_start = row * stride
        # filter byte = raw[row_start]
        for col in range(width):
            off = row_start + 1 + col * channels
            if channels >= 3:
                r, g, b = raw[off], raw[off+1], raw[off+2]
                if r > threshold or g > threshold or b > threshold:
                    non_black += 1
            else:
                if raw[off] > threshold:
                    non_black += 1
    return non_black, width * height


def _render_ass_frame(ass_text, out_png):
    """1080x1920 黒背景に ass_text を焼き込み、1フレームPNGを書き出す。"""
    ass_path = out_png.with_suffix(".ass")
    ass_path.write_text(ass_text, encoding="utf-8")
    cmd = [
        _FFMPEG_BIN, "-y",
        "-f", "lavfi", "-i", "color=c=black:s=1080x1920:d=1",
        "-vf", "ass={}:fontsdir={}".format(str(ass_path), _FONTS_DIR),
        "-frames:v", "1",
        str(out_png),
    ]
    proc = subprocess.run(cmd, capture_output=True, timeout=30)
    if proc.returncode != 0:
        raise AssertionError(
            "ffmpeg失敗 rc={} stderr={}".format(proc.returncode, proc.stderr.decode("utf-8", "replace")[-800:])
        )


@pytest.mark.skipif(not _ffmpeg_available(), reason="bin/ffmpeg not found")
def test_smoke_render_maru_pop_actually_draws_glyphs(tmp_path):
    """Zen Maru Gothic Black(maru-pop)で「テスト文字abc」が実際に描画される。"""
    pieces = [{"out_start": 0.0, "out_end": 1.0, "lines": ["テスト文字abc"],
               "emphasis": [], "style": "base"}]
    ass = subtitles.generate_ass_with_style(pieces, telop_style="maru-pop", animation_enabled=False)
    png = tmp_path / "maru.png"
    _render_ass_frame(ass, png)
    non_black, total = _count_non_black_pixels_png(png)
    # 大きな文字が描かれていれば数万ピクセル以上が白系になる。閾値は保守的に 300 とする。
    assert non_black > 300, "maru-popで文字が描画されていない: non_black={} total={}".format(non_black, total)


@pytest.mark.skipif(not _ffmpeg_available(), reason="bin/ffmpeg not found")
def test_smoke_render_pop_yellow_actually_draws_glyphs(tmp_path):
    """Mochiy Pop One(pop-yellow・band=true 座布団あり)でも描画される。"""
    pieces = [{"out_start": 0.0, "out_end": 1.0, "lines": ["テスト文字abc"],
               "emphasis": [], "style": "base"}]
    ass = subtitles.generate_ass_with_style(pieces, telop_style="pop-yellow", animation_enabled=False)
    png = tmp_path / "pop.png"
    _render_ass_frame(ass, png)
    non_black, total = _count_non_black_pixels_png(png)
    assert non_black > 300, "pop-yellowで文字が描画されていない: non_black={} total={}".format(non_black, total)


# ---------------------------------------------------------------------------
# 6. 直近回避（連続同スタイル防止）: history を渡した pick_telop_style_name の挙動
#
# 直近1件のstyleを候補プールから除外することで、seedが偶々近いmd5を出したときに
# 連続で同じスタイルが選ばれる問題を防ぐ。実装は pipeline/bgm_library と同じ
# アトミック書き（tempfile→os.replace）+ flock 排他パターンを使う。
# ---------------------------------------------------------------------------


def test_pick_telop_style_name_avoids_immediately_previous_style(tmp_path):
    """直前に採用したstyleは、次の別プロジェクトでは絶対に選ばれない。

    同じseedを渡しても filtered pool から除外される→別のstyleが返ることを検証。
    """
    _reload_telop_styles_cache()
    history_path = tmp_path / ".telop_history.json"
    history = {"recent": []}
    prev_style = subtitles.pick_telop_style_name(
        "seed-shared", preset=None, history=history,
        record_project_id="prj-prev", history_path_override=str(history_path),
    )
    # 同じseedでも別project_idなら直近styleは除外される
    next_style = subtitles.pick_telop_style_name(
        "seed-shared", preset=None, history=history,
        record_project_id="prj-next", history_path_override=str(history_path),
    )
    assert next_style != prev_style, (
        "直近styleが除外されていない: prev={} next={}".format(prev_style, next_style)
    )


def test_pick_telop_style_name_degrades_when_only_one_candidate(tmp_path):
    """horizontal_pool が 1個しか無い状況では直近回避を諦めて縮退する（そのstyleを返す）。"""
    _reload_telop_styles_cache()
    original_cache = subtitles._TELOP_STYLES_CACHE
    subtitles._TELOP_STYLES_CACHE = {
        "styles": {"only-one": dict(subtitles._DEFAULT_TELOP_STYLE_DEF)},
        "horizontal_pool": ["only-one"],
        "vertical_default": subtitles.VERTICAL_TELOP_STYLE_NAME,
    }
    try:
        history = {"recent": [
            {"project_id": "prj-prev", "name": "only-one", "ts": "2026-07-20T00:00:00"},
        ]}
        name = subtitles.pick_telop_style_name(
            "seed-any", preset=None, history=history,
            record_project_id="prj-next",
            history_path_override=str(tmp_path / ".telop_history.json"),
        )
        assert name == "only-one"
    finally:
        subtitles._TELOP_STYLES_CACHE = None


def test_pick_telop_style_name_same_project_rerun_is_deterministic_and_no_duplicate(tmp_path):
    """同一 project_id を再度渡すと同じstyleが返り、recent には1エントリだけ残る。

    自プロジェクトの過去エントリは回避対象から外す（bgm_library と同じ挙動）ため、
    直前に自分が記録したstyleでも再実行時は候補プールから外されない。append抑止に
    より recent は同 project につき最大1件。
    """
    _reload_telop_styles_cache()
    history_path = tmp_path / ".telop_history.json"
    history = {"recent": []}
    first = subtitles.pick_telop_style_name(
        "seed-a", preset=None, history=history,
        record_project_id="prj-1", history_path_override=str(history_path),
    )
    # 直後に同じ project_id で再実行（間に別プロジェクトを挟まない）→ 決定論維持
    second = subtitles.pick_telop_style_name(
        "seed-a", preset=None, history=history,
        record_project_id="prj-1", history_path_override=str(history_path),
    )
    assert first == second, "同 project の再実行で決定論が崩れた: {} -> {}".format(first, second)
    prj1_entries = [r for r in history["recent"] if r.get("project_id") == "prj-1"]
    assert len(prj1_entries) == 1, "prj-1の履歴が重複している: {}".format(prj1_entries)


def test_telop_history_is_written_as_valid_json_atomically(tmp_path):
    """複数回の pick 後、履歴ファイルは妥当な JSON として読み直せる（tempfile→os.replace）。"""
    _reload_telop_styles_cache()
    history_path = tmp_path / ".telop_history.json"
    history = {"recent": []}
    for i in range(5):
        subtitles.pick_telop_style_name(
            "seed-{}".format(i), preset=None, history=history,
            record_project_id="prj-{}".format(i),
            history_path_override=str(history_path),
        )
    assert history_path.exists()
    data = json.loads(history_path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    assert isinstance(data.get("recent"), list)
    ids = [r["project_id"] for r in data["recent"]]
    assert ids == ["prj-0", "prj-1", "prj-2", "prj-3", "prj-4"]
    # 各エントリに必須キー
    for r in data["recent"]:
        assert set(r.keys()) >= {"project_id", "name", "ts"}
        assert r["name"] in subtitles.load_telop_styles()["horizontal_pool"]


def test_pick_telop_style_name_backward_compatible_signature_without_history():
    """従来の呼び出し(history/record_project_id 省略)が既存挙動のまま動く。"""
    _reload_telop_styles_cache()
    a = subtitles.pick_telop_style_name("project-abc", preset=None)
    b = subtitles.pick_telop_style_name("project-abc", preset=None)
    assert a == b
    # vertical_hook は履歴と無関係に vertical-serif 固定
    assert subtitles.pick_telop_style_name("x", preset="vertical_hook") == "vertical-serif"


def test_pick_telop_style_name_records_project_id_and_style_name(tmp_path):
    """record_project_id を渡すと recent に (project_id, name, ts) 付きで追記される。"""
    _reload_telop_styles_cache()
    history_path = tmp_path / ".telop_history.json"
    history = {"recent": []}
    name = subtitles.pick_telop_style_name(
        "seed-x", preset=None, history=history,
        record_project_id="pid-1", history_path_override=str(history_path),
    )
    assert len(history["recent"]) == 1
    entry = history["recent"][0]
    assert entry["project_id"] == "pid-1"
    assert entry["name"] == name
    assert "ts" in entry and entry["ts"]
    assert name in subtitles.load_telop_styles()["horizontal_pool"]


def test_pick_telop_style_name_vertical_hook_does_not_record_history(tmp_path):
    """縦書きプリセットは履歴管理の対象外（load_telop_styles に依存しない即時return）。"""
    _reload_telop_styles_cache()
    history_path = tmp_path / ".telop_history.json"
    history = {"recent": []}
    name = subtitles.pick_telop_style_name(
        "seed-v", preset="vertical_hook", history=history,
        record_project_id="pid-v", history_path_override=str(history_path),
    )
    assert name == "vertical-serif"
    # vertical_hook経路は履歴に何も書き込まない（縦書きは横書きプールに影響しない）
    assert history["recent"] == []
    assert not history_path.exists()

# -*- coding: utf-8 -*-
"""Mockビジュアルバックエンド（必須・完全動作・無課金）。

`bin/ffmpeg` の `gradients` ソースフィルタでグラデーション背景を生成し、
`zoompan` フィルタで motion_preset に応じたパン/ズーム（Ken Burns風）を適用した
擬似クリップmp4を出力する。

キャプション本文(caption_jp)はここでは焼き込まない（render.py が ASS 字幕として
一本化して焼き込むため）。ショット識別用に、画面右上へ小さく半透明のショット番号
（例: "#1"）だけを drawtext で添える。

外部APIを一切使わないため課金なし・鍵不要で常にE2Eが通る（このプロジェクトの
自己検証はこのバックエンドで行う）。実写/イラスト等の具象生成は行わない
（抽象的なグラデーション+アイコン風テキストのみ。svg-figures-abstract-only の
教訓と同じ理由=具象イラストはAIに描かせると必ず崩れるため、そもそも狙わない）。

shot["image_path"] が実在する画像ファイルを指す場合のみ例外で、gradients生成の
代わりにその画像を入力にしてKen Burns風のズーム/パンを付ける（商品画像を主役にした
image-to-videoのMock版）。未指定/ファイル不在時は非エラーで従来のgradients生成に
フォールバックする。

Python 3.9 互換構文のみ。
"""
from __future__ import annotations

import os
import re
import subprocess

from pipeline.config import load_config, project_root
from pipeline.visual.base import VisualBackend, VisualBackendError

FPS = 30
OUT_W, OUT_H = 1080, 1920


def _mock_shot_has_person(shot):
    """shot が人物を含むか（reference_visual.has_person もしくは shot.has_person）。純関数。"""
    if not isinstance(shot, dict):
        return False
    rv = shot.get("reference_visual")
    if isinstance(rv, dict) and rv.get("has_person") is True:
        return True
    return shot.get("has_person") is True

# ショットごとに見た目を変えるためのグラデーション配色パレット（循環使用）。
_PALETTE = [
    ("0x1a2a6c", "0xfdbb2d"),  # 藍→金
    ("0x0f2027", "0x2c5364"),  # 深緑青系
    ("0x654ea3", "0xeaafc8"),  # 紫→ピンク
    ("0x11998e", "0x38ef7d"),  # 緑系
    ("0x8e2de2", "0x4a00e0"),  # 紫系
    ("0xee9ca7", "0xffdde1"),  # 桜系
]


def _palette_for(shot_id):
    idx = abs(hash(shot_id)) % len(_PALETTE) if shot_id else 0
    return _PALETTE[idx]


def _hex_to_ff_color(hex_str):
    """"#rrggbb" → ffmpeg gradients が受理する "0xRRGGBB"（アルファ無）。無効なら None。"""
    if not isinstance(hex_str, str):
        return None
    s = hex_str.strip().lower()
    if s.startswith("#"):
        s = s[1:]
    elif s.startswith("0x"):
        s = s[2:]
    if len(s) != 6 or any(ch not in "0123456789abcdef" for ch in s):
        return None
    return "0x" + s.upper()


def _palette_for_shot(shot):
    """shot の reference_visual.color_palette_hex を優先し、無ければ shot_id から
    _PALETTE の循環選択にフォールバックする。参考動画の実色を mock でも表現するため。
    """
    if isinstance(shot, dict):
        rv = shot.get("reference_visual")
        if isinstance(rv, dict):
            palette = rv.get("color_palette_hex") or []
            if isinstance(palette, list):
                converted = [c for c in (_hex_to_ff_color(x) for x in palette) if c]
                if len(converted) >= 2:
                    return converted[0], converted[1]
                if len(converted) == 1:
                    # 1色しか無ければ、_PALETTE の同idx色と組み合わせて自然なグラデにする
                    fallback = _palette_for(shot.get("id"))
                    return converted[0], fallback[1]
    return _palette_for((shot or {}).get("id") if isinstance(shot, dict) else None)


def _escape_drawtext(text):
    """drawtextフィルタのtext=値として安全な形にエスケープする（':' '\\' '%' 等）。"""
    text = text or ""
    text = text.replace("\\", "\\\\")
    text = text.replace(":", "\\:")
    text = text.replace("'", "’")
    text = text.replace("%", "\\%")
    return text


def _shot_number_label(shot_id):
    """shot_id（例: "s1" "s12"）から画面表示用の短いショット番号ラベルを作る。

    数字が取れなければidそのものを、idが無ければ空文字を返す（=表示しない）。
    """
    if not shot_id:
        return ""
    m = re.search(r"(\d+)", str(shot_id))
    return "#{}".format(m.group(1)) if m else "#{}".format(shot_id)


def _motion_exprs(motion_preset, total_frames, intensity=None):
    """motion_presetごとの zoompan z/x/y 式を返す。

    R2b F4: intensity ("weak" | "strong" | None) で強度を倍率化。既定 1.0、
    weak=0.65、strong=1.5。ken_burns の場合は Z 変化の amount も同倍率で拡縮する。
    従来（intensity=None）の挙動は 1.0 倍のまま維持（回帰なし）。
    """
    t = total_frames if total_frames > 0 else 1
    intensity_val = 1.0
    if isinstance(intensity, str):
        low = intensity.strip().lower()
        if low == "strong":
            intensity_val = 1.5
        elif low == "weak":
            intensity_val = 0.65
    # zoom 上限/下限の変化量を intensity で拡縮
    zoom_delta = round(0.3 * intensity_val, 3)
    pan_zoom = round(1.15, 3)  # pan の zoom base は動かさない（画枠外を作らないため）
    kb_zoom_delta = round(0.2 * intensity_val, 3)
    kb_pan_frac = round(0.5 * intensity_val, 3)
    if motion_preset == "zoom_in":
        z = "min(1+{d}*on/{t},{u})".format(d=zoom_delta, t=t, u=round(1 + zoom_delta, 3))
        x = "(iw-iw/zoom)/2"
        y = "(ih-ih/zoom)/2"
    elif motion_preset == "zoom_out":
        z = "max({u}-{d}*on/{t},1.0)".format(d=zoom_delta, t=t, u=round(1 + zoom_delta, 3))
        x = "(iw-iw/zoom)/2"
        y = "(ih-ih/zoom)/2"
    elif motion_preset == "pan_left":
        z = "{}".format(pan_zoom)
        x = "(iw-iw/zoom)*(1-on/{t})".format(t=t)
        y = "(ih-ih/zoom)/2"
    elif motion_preset == "pan_right":
        z = "{}".format(pan_zoom)
        x = "(iw-iw/zoom)*(on/{t})".format(t=t)
        y = "(ih-ih/zoom)/2"
    elif motion_preset == "ken_burns":
        z = "min(1+{d}*on/{t},{u})".format(d=kb_zoom_delta, t=t, u=round(1 + kb_zoom_delta, 3))
        x = "(iw-iw/zoom)*(on/{t})*{p}".format(t=t, p=kb_pan_frac)
        y = "(ih-ih/zoom)/2"
    else:  # static
        z = "1"
        x = "(iw-iw/zoom)/2"
        y = "(ih-ih/zoom)/2"
    return z, x, y


def build_mock_cmd(ffmpeg_bin, shot, out_path, fonts_dir=None):
    """1ショット分のMock擬似クリップを生成するffmpegコマンド配列を構築する（副作用なし・テスト対象）。

    shot["image_path"] が実在するファイルを指す場合は、gradientsソースの代わりに
    その画像を入力にして(-loop 1 -i <image>)、1080x1920へcrop（force_original_aspect_ratio=
    increase後にcrop=中央）した上で、通常どおり motion_preset に応じた zoompan（Ken Burns風）
    を適用する。image_path未指定/ファイル不在時は従来どおりgradients生成にフォールバック
    する（非エラー。mockは常に無課金・鍵不要でE2Eが通ることを優先する設計のため）。
    """
    duration = float(shot.get("duration_sec", 5.0))
    total_frames = max(1, int(round(duration * FPS)))
    motion_preset = shot.get("motion_preset", "static")
    shot_id = shot.get("id", "")
    number_label = _shot_number_label(shot_id)

    intensity = shot.get("motion_intensity")
    if not intensity:
        rv = shot.get("reference_visual") if isinstance(shot.get("reference_visual"), dict) else None
        if rv:
            intensity = rv.get("intensity")
    z, x, y = _motion_exprs(motion_preset, total_frames, intensity=intensity)

    fonts_dir = fonts_dir or str(project_root() / "assets" / "fonts")
    font_path = fonts_dir.rstrip("/") + "/NotoSansJP-Black.ttf"

    image_path = shot.get("image_path")
    use_image = bool(image_path) and os.path.isfile(image_path)

    pre_filters = []
    if use_image:
        input_args = ["-loop", "1", "-i", str(image_path)]
        pre_filters = [
            "scale={w}:{h}:force_original_aspect_ratio=increase".format(w=OUT_W, h=OUT_H),
            "crop={w}:{h}".format(w=OUT_W, h=OUT_H),
        ]
    else:
        c0, c1 = _palette_for_shot(shot)
        gradients_src = (
            "gradients=size={w}x{h}:rate={fps}:speed=0.03:c0={c0}:c1={c1}:x0=0:y0=0:x1={w}:y1={h}"
        ).format(w=OUT_W, h=OUT_H, fps=FPS, c0=c0, c1=c1)
        input_args = ["-f", "lavfi", "-i", gradients_src]

    vf_parts = pre_filters + [
        "zoompan=z='{z}':x='{x}':y='{y}':d=1:s={w}x{h}:fps={fps}".format(z=z, x=x, y=y, w=OUT_W, h=OUT_H, fps=FPS),
        "setsar=1",
        "format=yuv420p",
    ]
    if number_label:
        vf_parts.append(
            "drawtext=fontfile='{font}':text='{text}':fontsize=28:fontcolor=white@0.5:"
            "x=w-text_w-24:y=24".format(
                font=font_path, text=_escape_drawtext(number_label)
            )
        )
    vf = ",".join(vf_parts)

    cmd = [
        ffmpeg_bin, "-y",
    ] + input_args + [
        "-t", "{:.3f}".format(duration),
        "-vf", vf,
        "-r", str(FPS),
        "-c:v", "libx264", "-crf", "20", "-preset", "veryfast",
        "-an",
        out_path,
    ]
    return cmd


class MockBackend(VisualBackend):
    name = "mock"

    def __init__(self, cfg=None):
        super().__init__(cfg)
        # persona_anchor は mock では実フレーム抽出をせず「メタ記録のみ」（診断対策の
        # 動作確認用）。sequential generate を通じて最初の人物 shot を捕捉扱いにし、以降の
        # 人物 shot を適用扱いとして meta に記す。実際の identity 統一は higgsfield_backend 側。
        self.persona_consistency = bool((self.cfg.get("visual") or {}).get("persona_consistency", True))
        self._persona_seen = False

    def generate(self, shot: dict, out_path: str) -> dict:
        cfg = self.cfg or load_config()
        ffmpeg_bin = cfg.get("ffmpeg_bin") or str(project_root() / "bin" / "ffmpeg")
        fonts_dir = str(project_root() / "assets" / "fonts")
        cmd = build_mock_cmd(ffmpeg_bin, shot, out_path, fonts_dir=fonts_dir)
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False)
        if proc.returncode != 0:
            raise VisualBackendError(
                "mock backend: ffmpeg生成に失敗しました (shot_id={}): {}".format(
                    shot.get("id"), proc.stderr.decode("utf-8", "replace")[:500]
                )
            )
        meta = {"backend": self.name, "shot_id": shot.get("id"), "out_path": out_path}
        # 商品1本ロック検証用のメタ記録（mockは実映像を作らないためプロンプト/参照の証跡のみ）。
        ref_imgs = shot.get("reference_images") or []
        if isinstance(ref_imgs, list) and ref_imgs:
            meta["reference_images"] = list(ref_imgs)
        if shot.get("image_path"):
            meta["image_path"] = shot.get("image_path")
        _vp_low = (shot.get("visual_prompt") or "").lower() if isinstance(shot.get("visual_prompt"), str) else ""
        if "the exact product shown in the reference image" in _vp_low:
            meta["product_locked"] = True
        elif "do not show any product bottle" in _vp_low:
            meta["no_product_directive"] = True
        person_shot = bool(self.persona_consistency) and _mock_shot_has_person(shot)
        if person_shot:
            if self._persona_seen:
                meta["persona_anchor"] = "applied"
            else:
                meta["persona_anchor"] = "captured"
                self._persona_seen = True
        else:
            meta["persona_anchor"] = "none"
        return meta

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

Python 3.9 互換構文のみ。
"""
from __future__ import annotations

import re
import subprocess

from pipeline.config import load_config, project_root
from pipeline.visual.base import VisualBackend, VisualBackendError

FPS = 30
OUT_W, OUT_H = 1080, 1920

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


def _motion_exprs(motion_preset, total_frames):
    """motion_presetごとの zoompan z/x/y 式を返す。"""
    t = total_frames if total_frames > 0 else 1
    if motion_preset == "zoom_in":
        z = "min(1+0.3*on/{t},1.3)".format(t=t)
        x = "(iw-iw/zoom)/2"
        y = "(ih-ih/zoom)/2"
    elif motion_preset == "zoom_out":
        z = "max(1.3-0.3*on/{t},1.0)".format(t=t)
        x = "(iw-iw/zoom)/2"
        y = "(ih-ih/zoom)/2"
    elif motion_preset == "pan_left":
        z = "1.15"
        x = "(iw-iw/zoom)*(1-on/{t})".format(t=t)
        y = "(ih-ih/zoom)/2"
    elif motion_preset == "pan_right":
        z = "1.15"
        x = "(iw-iw/zoom)*(on/{t})".format(t=t)
        y = "(ih-ih/zoom)/2"
    elif motion_preset == "ken_burns":
        z = "min(1+0.2*on/{t},1.2)".format(t=t)
        x = "(iw-iw/zoom)*(on/{t})*0.5".format(t=t)
        y = "(ih-ih/zoom)/2"
    else:  # static
        z = "1"
        x = "(iw-iw/zoom)/2"
        y = "(ih-ih/zoom)/2"
    return z, x, y


def build_mock_cmd(ffmpeg_bin, shot, out_path, fonts_dir=None):
    """1ショット分のMock擬似クリップを生成するffmpegコマンド配列を構築する（副作用なし・テスト対象）。"""
    duration = float(shot.get("duration_sec", 5.0))
    total_frames = max(1, int(round(duration * FPS)))
    motion_preset = shot.get("motion_preset", "static")
    shot_id = shot.get("id", "")
    number_label = _shot_number_label(shot_id)

    c0, c1 = _palette_for(shot_id)
    z, x, y = _motion_exprs(motion_preset, total_frames)

    fonts_dir = fonts_dir or str(project_root() / "assets" / "fonts")
    font_path = fonts_dir.rstrip("/") + "/NotoSansJP-Black.ttf"

    gradients_src = (
        "gradients=size={w}x{h}:rate={fps}:speed=0.03:c0={c0}:c1={c1}:x0=0:y0=0:x1={w}:y1={h}"
    ).format(w=OUT_W, h=OUT_H, fps=FPS, c0=c0, c1=c1)

    vf_parts = [
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
        "-f", "lavfi", "-i", gradients_src,
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
        return {"backend": self.name, "shot_id": shot.get("id"), "out_path": out_path}

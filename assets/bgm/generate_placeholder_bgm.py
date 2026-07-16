# -*- coding: utf-8 -*-
"""検証用の合成BGMプレースホルダーを生成する開発者向けツール。

外部から楽曲を一切ダウンロードせず、ffmpegの正弦波ジェネレータ(sine)のみで
コード進行（和音の移り変わり）を持つ音源を合成する。ライセンスリスクはゼロ。

生成物は manifest.json に license="synthetic-placeholder" として登録され、
BGM＋ダッキング機能の実機検証に使う。本番配布では、ユーザー自身がライセンスの
明確な楽曲ファイルに差し替えることを前提とする（本フォルダの README.md 参照）。

使い方:
    "<repo>/.venv/bin/python3" "assets/bgm/generate_placeholder_bgm.py"

Python 3.9 互換構文のみ。
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_FFMPEG = _HERE.parent.parent / "bin" / "ffmpeg"

# 各moodのコード進行（周波数Hz、4和音サイクル）とテンポ感（1和音の長さ秒）。
# ダッキング検証のため、和音は変化させつつも全体の音量（RMS/ラウドネス）が
# ほぼ一定に保たれるよう、tremolo等の周期的な音量変調は使わない。
_MOODS = {
    "upbeat": {
        # C - G - Am - F（ポップ王道進行）、明るい高めのレジスタ、テンポ速め
        "chords": [
            [261.63, 329.63, 392.00, 523.25],  # C
            [196.00, 246.94, 293.66, 392.00],  # G
            [220.00, 261.63, 329.63, 440.00],  # Am
            [174.61, 220.00, 261.63, 349.23],  # F
        ],
        "chord_dur": 2.0,
        "cycles": 12,  # 4和音 * 2.0秒 * 12サイクル = 96秒
        "gain": 0.22,
    },
    "calm": {
        # C - F - Am - G、低めのレジスタ、和音が長く続く落ち着いたパッド
        "chords": [
            [130.81, 164.81, 196.00],  # C
            [174.61, 220.00, 261.63],  # F
            [220.00, 261.63, 329.63],  # Am
            [196.00, 246.94, 293.66],  # G
        ],
        "chord_dur": 6.0,
        "cycles": 4,  # 4 * 6.0 * 4 = 96秒
        "gain": 0.20,
    },
    "emotional": {
        # Am - F - C - G（感傷的な定番進行）、中音域、中テンポ
        "chords": [
            [220.00, 261.63, 329.63],  # Am
            [174.61, 220.00, 261.63],  # F
            [130.81, 164.81, 196.00],  # C
            [196.00, 246.94, 293.66],  # G
        ],
        "chord_dur": 4.0,
        "cycles": 6,  # 4 * 4.0 * 6 = 96秒
        "gain": 0.20,
    },
}


def _build_chord_segment(freqs, duration, gain, out_path):
    """複数の正弦波(和音の各音)を合成し、始終端に20msフェードを付けた1和音分のwavを作る。"""
    n = len(freqs)
    inputs = []
    for f in freqs:
        inputs += [
            "-f", "lavfi",
            "-i", "sine=frequency={:.3f}:duration={:.3f}:sample_rate=48000".format(f, duration),
        ]
    labels = "".join("[{}:a]".format(i) for i in range(n))
    fade_st = max(duration - 0.02, 0.0)
    filt = (
        "{labels}amix=inputs={n}:duration=longest:normalize=0,"
        "volume={gain},"
        "afade=t=in:st=0:d=0.02,afade=t=out:st={fade_st:.3f}:d=0.02"
    ).format(labels=labels, n=n, gain=gain, fade_st=fade_st)
    cmd = [str(_FFMPEG), "-y"] + inputs + [
        "-filter_complex", filt, "-ac", "2", "-ar", "48000", str(out_path),
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def generate_mood(mood, out_mp3_path):
    spec = _MOODS[mood]
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        seg_paths = []
        idx = 0
        for _cycle in range(spec["cycles"]):
            for chord in spec["chords"]:
                seg_path = tmp / "seg{:03d}.wav".format(idx)
                _build_chord_segment(chord, spec["chord_dur"], spec["gain"], seg_path)
                seg_paths.append(seg_path)
                idx += 1

        list_path = tmp / "list.txt"
        list_path.write_text(
            "\n".join("file '{}'".format(str(p).replace("'", "'\\''")) for p in seg_paths) + "\n",
            encoding="utf-8",
        )
        concat_wav = tmp / "concat.wav"
        cmd = [
            str(_FFMPEG), "-y", "-f", "concat", "-safe", "0", "-i", str(list_path),
            "-c", "copy", str(concat_wav),
        ]
        subprocess.run(cmd, check=True, capture_output=True)

        out_mp3_path.parent.mkdir(parents=True, exist_ok=True)
        cmd2 = [
            str(_FFMPEG), "-y", "-i", str(concat_wav),
            "-af", "loudnorm=I=-18:TP=-2.0:LRA=7:print_format=summary",
            "-codec:a", "libmp3lame", "-b:a", "192k",
            str(out_mp3_path),
        ]
        subprocess.run(cmd2, check=True, capture_output=True)


def main():
    for mood in ("upbeat", "calm", "emotional"):
        out_path = _HERE / "{}_01.mp3".format(mood)
        print("[generate] {} -> {}".format(mood, out_path))
        generate_mood(mood, out_path)
    print("done")


if __name__ == "__main__":
    main()

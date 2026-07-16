# -*- coding: utf-8 -*-
"""検証用の合成SFX(効果音)プレースホルダーを生成する開発者向けツール。

assets/bgm/generate_placeholder_bgm.py と同じ方針: 外部から音源を一切ダウンロードせず、
ffmpegのシグナルジェネレータ（sine/aevalsrc/anoisesrc）のみで合成する。ライセンスリスクはゼロ。

生成物は manifest.json に license="synthetic-placeholder" として登録され、
Reel Studio の SFXオーバーレイ機能の実機検証に使う。本番配布では、ユーザー自身が
ライセンスの明確な効果音ファイルに差し替えることを前提とする。

8種（各0.3〜1.5秒、-14LUFS目安）:
  whoosh  - 下降チャープ（場面転換の「シュッ」）
  swoosh  - 上昇チャープ（whooshと逆方向、動きの強調）
  pop     - 短い減衰音（ポップ/選択音）
  ding    - ベル風の和音（達成/通知音）
  click   - ノイズバースト（クリック/タップ音）
  riser   - クレッシェンド付き上昇チャープ（盛り上げ/カウントダウン）
  impact  - 低音の打撃音（インパクト/強調）
  sparkle - トレモロ付き高音シマー（キラキラ演出）

使い方:
    "<repo>/.venv/bin/python3" "assets/sfx/generate_placeholder_sfx.py"

Python 3.9 互換構文のみ。
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_FFMPEG = _HERE.parent.parent / "bin" / "ffmpeg"

_TARGET_LOUDNESS_I = -14.0
_SAMPLE_RATE = 48000

_LICENSE_TEXT = (
    "synthetic-placeholder (generate_placeholder_sfx.py で ffmpeg 信号合成。著作権フリー。"
    "SFXオーバーレイ機能の実機検証用。本番配布前にライセンスの明確な効果音へ差し替えてください)"
)


def _chirp_expr(f0, f1, duration):
    """0〜durationで周波数がf0からf1へ線形に変化する近似チャープのsin式（aevalsrc用）。"""
    return "sin(2*PI*({f0}+({f1}-{f0})*t/{dur})*t)".format(f0=f0, f1=f1, dur=duration)


def _run(cmd):
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg failed: {}\n{}".format(" ".join(cmd), proc.stderr.decode("utf-8", "replace")[-2000:]))
    return proc


def _finalize(out_path, filter_complex, extra_inputs):
    """filter_complexの最終ラベルを[aout]として、loudnorm+wavエンコードまで実行する共通末尾。"""
    cmd = [str(_FFMPEG), "-y"] + extra_inputs + [
        "-filter_complex", filter_complex,
        "-map", "[aout]",
        "-ac", "2", "-ar", str(_SAMPLE_RATE),
        "-c:a", "pcm_s16le",
        str(out_path),
    ]
    _run(cmd)


def gen_whoosh(out_path):
    duration = 0.6
    expr = _chirp_expr(900, 250, duration)
    inputs = ["-f", "lavfi", "-i", "aevalsrc={}:s={}:d={}".format(expr, _SAMPLE_RATE, duration)]
    filt = (
        "[0:a]afade=t=in:st=0:d=0.02,afade=t=out:st={fade_st:.3f}:d=0.2,volume=0.9,"
        "loudnorm=I={ti}:TP=-1.5:LRA=7:print_format=summary[aout]"
    ).format(fade_st=duration - 0.2, ti=_TARGET_LOUDNESS_I)
    _finalize(out_path, filt, inputs)


def gen_swoosh(out_path):
    duration = 0.5
    expr = _chirp_expr(300, 1400, duration)
    inputs = ["-f", "lavfi", "-i", "aevalsrc={}:s={}:d={}".format(expr, _SAMPLE_RATE, duration)]
    filt = (
        "[0:a]afade=t=in:st=0:d=0.02,afade=t=out:st={fade_st:.3f}:d=0.15,volume=0.9,"
        "loudnorm=I={ti}:TP=-1.5:LRA=7:print_format=summary[aout]"
    ).format(fade_st=duration - 0.15, ti=_TARGET_LOUDNESS_I)
    _finalize(out_path, filt, inputs)


def gen_pop(out_path):
    duration = 0.3
    inputs = ["-f", "lavfi", "-i", "sine=frequency=180:duration={}:sample_rate={}".format(duration, _SAMPLE_RATE)]
    filt = (
        "[0:a]afade=t=in:st=0:d=0.005,afade=t=out:st={fade_st:.3f}:d=0.25,volume=0.9,"
        "loudnorm=I={ti}:TP=-1.5:LRA=7:print_format=summary[aout]"
    ).format(fade_st=duration - 0.25, ti=_TARGET_LOUDNESS_I)
    _finalize(out_path, filt, inputs)


def gen_ding(out_path):
    duration = 1.2
    inputs = [
        "-f", "lavfi", "-i", "sine=frequency=1318.5:duration={}:sample_rate={}".format(duration, _SAMPLE_RATE),
        "-f", "lavfi", "-i", "sine=frequency=2637.0:duration={}:sample_rate={}".format(duration, _SAMPLE_RATE),
    ]
    filt = (
        "[0:a][1:a]amix=inputs=2:duration=longest:normalize=0[mixed];"
        "[mixed]afade=t=in:st=0:d=0.01,afade=t=out:st={fade_st:.3f}:d=1.0,volume=0.8,"
        "loudnorm=I={ti}:TP=-1.5:LRA=7:print_format=summary[aout]"
    ).format(fade_st=duration - 1.0, ti=_TARGET_LOUDNESS_I)
    _finalize(out_path, filt, inputs)


def gen_click(out_path):
    duration = 0.3
    inputs = ["-f", "lavfi", "-i", "anoisesrc=color=white:duration={}:sample_rate={}".format(duration, _SAMPLE_RATE)]
    filt = (
        "[0:a]highpass=f=2000,afade=t=in:st=0:d=0.002,afade=t=out:st={fade_st:.3f}:d=0.25,volume=0.8,"
        "loudnorm=I={ti}:TP=-1.5:LRA=7:print_format=summary[aout]"
    ).format(fade_st=duration - 0.25, ti=_TARGET_LOUDNESS_I)
    _finalize(out_path, filt, inputs)


def gen_riser(out_path):
    duration = 1.5
    expr = _chirp_expr(200, 2200, duration)
    inputs = ["-f", "lavfi", "-i", "aevalsrc={}:s={}:d={}".format(expr, _SAMPLE_RATE, duration)]
    vol_expr = "if(lt(t,{dur}),0.15+(0.9-0.15)*t/{dur},0.9)".format(dur=duration)
    filt = (
        "[0:a]volume='{vexpr}':eval=frame,afade=t=out:st={fade_st:.3f}:d=0.1,"
        "loudnorm=I={ti}:TP=-1.5:LRA=7:print_format=summary[aout]"
    ).format(vexpr=vol_expr, fade_st=duration - 0.1, ti=_TARGET_LOUDNESS_I)
    _finalize(out_path, filt, inputs)


def gen_impact(out_path):
    duration = 0.45
    inputs = [
        "-f", "lavfi", "-i", "sine=frequency=65:duration={}:sample_rate={}".format(duration, _SAMPLE_RATE),
        "-f", "lavfi", "-i", "anoisesrc=color=brown:duration={}:sample_rate={}".format(duration, _SAMPLE_RATE),
    ]
    filt = (
        "[1:a]volume=0.15[noise];"
        "[0:a][noise]amix=inputs=2:duration=longest:normalize=0[mixed];"
        "[mixed]afade=t=in:st=0:d=0.005,afade=t=out:st={fade_st:.3f}:d=0.35,volume=0.95,"
        "loudnorm=I={ti}:TP=-1.5:LRA=7:print_format=summary[aout]"
    ).format(fade_st=duration - 0.35, ti=_TARGET_LOUDNESS_I)
    _finalize(out_path, filt, inputs)


def gen_sparkle(out_path):
    duration = 1.0
    inputs = [
        "-f", "lavfi", "-i", "sine=frequency=2200:duration={}:sample_rate={}".format(duration, _SAMPLE_RATE),
        "-f", "lavfi", "-i", "sine=frequency=3300:duration={}:sample_rate={}".format(duration, _SAMPLE_RATE),
        "-f", "lavfi", "-i", "sine=frequency=4400:duration={}:sample_rate={}".format(duration, _SAMPLE_RATE),
    ]
    filt = (
        "[0:a][1:a][2:a]amix=inputs=3:duration=longest:normalize=0[mixed];"
        "[mixed]tremolo=f=9:d=0.6,afade=t=in:st=0:d=0.02,afade=t=out:st={fade_st:.3f}:d=0.4,volume=0.6,"
        "loudnorm=I={ti}:TP=-1.5:LRA=7:print_format=summary[aout]"
    ).format(fade_st=duration - 0.4, ti=_TARGET_LOUDNESS_I)
    _finalize(out_path, filt, inputs)


_GENERATORS = {
    "whoosh": (gen_whoosh, "ホワッシュ（場面転換）"),
    "swoosh": (gen_swoosh, "スウォッシュ（動きの強調）"),
    "pop": (gen_pop, "ポップ（選択/決定）"),
    "ding": (gen_ding, "ディン（達成/通知）"),
    "click": (gen_click, "クリック（タップ/操作）"),
    "riser": (gen_riser, "ライザー（盛り上げ）"),
    "impact": (gen_impact, "インパクト（強調/衝撃）"),
    "sparkle": (gen_sparkle, "スパークル（キラキラ演出）"),
}


def main():
    manifest = []
    for name, (fn, label_jp) in _GENERATORS.items():
        out_path = _HERE / "{}_01.wav".format(name)
        print("[generate] {} -> {}".format(name, out_path))
        fn(out_path)
        proc = subprocess.run(
            [str(_FFMPEG.parent / "ffprobe"), "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(out_path)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            duration = round(float(proc.stdout.decode("utf-8", "replace").strip()), 3)
        except Exception:
            duration = None
        manifest.append({
            "file": out_path.name,
            "label_jp": label_jp,
            "duration": duration,
            "license": _LICENSE_TEXT,
        })

    manifest_path = _HERE / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("manifest -> {}".format(manifest_path))
    print("done")


if __name__ == "__main__":
    main()

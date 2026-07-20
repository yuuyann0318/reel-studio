# -*- coding: utf-8 -*-
"""スターターBGM 12曲(4mood × 3パターン)を assets/bgm/library/ に生成し manifest 追記。

外部楽曲を一切ダウンロードせず ffmpeg(sine / aevalsrc / anoisesrc)のみで合成する。
著作権リスクゼロ。sine単音より明確な音楽性を持たせるため:
  - コード進行(I-V-vi-IV / vi-IV-I-V 等)を三和音〜四和音のサインで積む
  - オクターブ下のベースラインを別トラックで薄く重ねる
  - upbeat/dramatic は aevalsrc でキック(低周波サイン+指数減衰)を beat 単位で叩く
  - upbeat は anoisesrc でハイハット(白色ノイズ + off-beat エンベロープ)を薄く重ねる
  - テンポは 90 / 100 / 110 / 120 / 128 bpm を mood ごとに使い分け
  - 各トラック 32秒。loudnorm I=-18 で AAC/m4a に格納

使い方:
    "<repo>/.venv/bin/python3" "assets/bgm/generate_starter_pack.py"

Python 3.9 互換のみ。
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent.parent
_FFMPEG = _REPO / "bin" / "ffmpeg"
_LIBRARY = _HERE / "library"
_MANIFEST = _HERE / "manifest.json"

# ノート → 周波数(A4=440Hz平均律)
_A4 = 440.0

_NOTE_SEMITONES = {
    "C": -9, "C#": -8, "Db": -8, "D": -7, "D#": -6, "Eb": -6,
    "E": -5, "F": -4, "F#": -3, "Gb": -3, "G": -2, "G#": -1, "Ab": -1,
    "A": 0, "A#": 1, "Bb": 1, "B": 2,
}


def _hz(note, octave=4):
    """ノート名+オクターブ → Hz。'C4'相当を C4=261.63Hz として算出。"""
    semi = _NOTE_SEMITONES[note] + (octave - 4) * 12
    return _A4 * (2.0 ** (semi / 12.0))


# コード = 三和音(root, third, fifth)。テンプレでオクターブを持たせる。
def _chord(root_name, kind, octave=4):
    """chord: (root, third, fifth) の3音を返す。kind='maj'/'min'。"""
    root = _hz(root_name, octave)
    if kind == "maj":
        third = root * (2.0 ** (4 / 12.0))  # 長3度
    else:
        third = root * (2.0 ** (3 / 12.0))  # 短3度
    fifth = root * (2.0 ** (7 / 12.0))
    return (root, third, fifth)


# --- 12トラックの設計 ---
# 各要素: (slug, mood, bpm, [(root, kind), ...4chords], chord_octave, use_drums, use_hihat)
_TRACKS = [
    # UPBEAT (4mood x 3variation)
    ("upbeat_bright_01",  "upbeat",   128, [("C", "maj"), ("G", "maj"), ("A", "min"), ("F", "maj")], 4, True,  True),
    ("upbeat_house_02",   "upbeat",   120, [("F", "maj"), ("C", "maj"), ("D", "min"), ("Bb","maj")], 4, True,  True),
    ("upbeat_pop_03",     "upbeat",   110, [("G", "maj"), ("D", "maj"), ("E", "min"), ("C", "maj")], 4, True,  False),
    # CALM (drums off, slow)
    ("calm_ambient_01",   "calm",      80, [("C", "maj"), ("F", "maj"), ("A", "min"), ("G", "maj")], 4, False, False),
    ("calm_lofi_02",      "calm",      85, [("A", "min"), ("E", "min"), ("F", "maj"), ("C", "maj")], 3, False, False),
    ("calm_pad_03",       "calm",      90, [("D", "maj"), ("A", "maj"), ("B", "min"), ("G", "maj")], 4, False, False),
    # EMOTIONAL (minor progressions, mid-slow, no kick)
    ("emotional_piano_01",  "emotional",  95, [("A", "min"), ("F", "maj"), ("C", "maj"), ("G", "maj")], 4, False, False),
    ("emotional_strings_02","emotional", 100, [("E", "min"), ("C", "maj"), ("G", "maj"), ("D", "maj")], 4, False, False),
    ("emotional_slow_03",   "emotional",  90, [("D", "min"), ("Bb","maj"), ("F", "maj"), ("C", "maj")], 4, False, False),
    # DRAMATIC (low, deep kick, minor)
    ("dramatic_hits_01",  "dramatic", 110, [("D", "min"), ("A", "min"), ("Bb","maj"), ("F", "maj")], 3, True,  False),
    ("dramatic_taiko_02", "dramatic", 100, [("E", "min"), ("C", "maj"), ("A", "min"), ("B", "min")], 3, True,  False),
    ("dramatic_epic_03",  "dramatic", 120, [("G", "min"), ("D", "min"), ("Eb","maj"), ("Bb","maj")], 3, True,  False),
]


def track_specs():
    """テスト等から呼ぶ用: 現在のスターター12曲仕様を返す。"""
    return list(_TRACKS)


def _run(cmd):
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(
            "ffmpeg failed: {}\nSTDERR:\n{}".format(
                " ".join(str(x) for x in cmd[:6]) + " ...",
                r.stderr.decode("utf-8", errors="replace")[-2000:],
            )
        )


def _sine_input(freq, duration):
    """一音のサイン波を1入力として構成するための CLI 引数リスト。"""
    return [
        "-f", "lavfi",
        "-i", "sine=frequency={:.4f}:duration={:.4f}:sample_rate=48000".format(freq, duration),
    ]


def _build_chord_wav(freqs, duration, gain, out_path):
    """三和音+末端フェードを 1 セグ wav に書き出す。"""
    n = len(freqs)
    inputs = []
    for f in freqs:
        inputs += _sine_input(f, duration)
    labels = "".join("[{}:a]".format(i) for i in range(n))
    fade_st = max(duration - 0.03, 0.0)
    filt = (
        "{labels}amix=inputs={n}:duration=longest:normalize=0,"
        "volume={gain},"
        "afade=t=in:st=0:d=0.03,afade=t=out:st={fade_st:.4f}:d=0.03"
    ).format(labels=labels, n=n, gain=gain, fade_st=fade_st)
    _run([str(_FFMPEG), "-y"] + inputs + [
        "-filter_complex", filt, "-ac", "2", "-ar", "48000", str(out_path),
    ])


def _build_bass_wav(root_hz, duration, gain, out_path):
    """コード進行のルート音をオクターブ下で薄く鳴らすベース。"""
    freq = root_hz / 2.0  # 1oct下
    fade_st = max(duration - 0.05, 0.0)
    filt = (
        "[0:a]volume={gain},"
        "afade=t=in:st=0:d=0.04,afade=t=out:st={fade_st:.4f}:d=0.04"
    ).format(gain=gain, fade_st=fade_st)
    _run([str(_FFMPEG), "-y"] + _sine_input(freq, duration) + [
        "-filter_complex", filt, "-ac", "2", "-ar", "48000", str(out_path),
    ])


def _build_kick_wav(bpm, duration, gain, out_path):
    """低周波サイン+指数減衰でキック。beat = 60/bpm ごとに叩く。

    aevalsrc の 'exprs' に mod(t, beat) を使い、beat 周期のパルスエンベロープを作る。
    """
    beat = 60.0 / bpm
    # 60Hz サイン * exp(-mod(t,beat)*22) : 各拍の頭でアタック、~150msで減衰
    expr = "sin(2*PI*60*t)*exp(-mod(t\\,{beat:.6f})*22)".format(beat=beat)
    _run([
        str(_FFMPEG), "-y",
        "-f", "lavfi",
        "-i", "aevalsrc=exprs='{expr}':d={dur:.4f}:s=48000:c=stereo".format(expr=expr, dur=duration),
        "-af", "volume={:.4f}".format(gain),
        "-ac", "2", "-ar", "48000", str(out_path),
    ])


def _build_hihat_wav(bpm, duration, gain, out_path):
    """白色ノイズを off-beat (拍の裏)でパルス化してハイハットに。"""
    beat = 60.0 / bpm
    half = beat / 2.0
    _run([
        str(_FFMPEG), "-y",
        "-f", "lavfi",
        "-i", "anoisesrc=color=white:duration={dur:.4f}:sample_rate=48000:amplitude=0.6".format(dur=duration),
        "-af",
        "volume='{g:.4f}*exp(-(mod(t+{half:.6f}\\,{beat:.6f}))*35)':eval=frame,"
        "highpass=f=6000".format(g=gain, half=half, beat=beat),
        "-ac", "2", "-ar", "48000", str(out_path),
    ])


def _concat_wavs(paths, out_path):
    """複数 wav を concat（同一 codec/rate を前提）。"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as f:
        for p in paths:
            f.write("file '{}'\n".format(str(p).replace("'", "'\\''")))
        list_path = f.name
    try:
        _run([
            str(_FFMPEG), "-y", "-f", "concat", "-safe", "0", "-i", list_path,
            "-c", "copy", str(out_path),
        ])
    finally:
        try:
            Path(list_path).unlink()
        except OSError:
            pass


def _mix_and_loudnorm(inputs, out_m4a):
    """複数 wav を amix → loudnorm → AAC(m4a) に書き出す。"""
    cli_inputs = []
    for p in inputs:
        cli_inputs += ["-i", str(p)]
    n = len(inputs)
    labels = "".join("[{}:a]".format(i) for i in range(n))
    filt = (
        "{labels}amix=inputs={n}:duration=longest:normalize=0,"
        "loudnorm=I=-18:TP=-2.0:LRA=7"
    ).format(labels=labels, n=n)
    _run([
        str(_FFMPEG), "-y"] + cli_inputs + [
        "-filter_complex", filt, "-c:a", "aac", "-b:a", "192k",
        "-ar", "48000", "-ac", "2", str(out_m4a),
    ])


def _build_track(spec, out_m4a):
    slug, mood, bpm, chords, octave, use_drums, use_hihat = spec
    # 1コード = 2小節 = 8拍。8拍 * (60/bpm) = 1コードの秒数
    chord_dur = 8.0 * 60.0 / bpm
    cycles = max(1, int(round(32.0 / (chord_dur * 4))))
    total_dur = chord_dur * 4 * cycles

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        # (a) chords
        chord_segs = []
        bass_segs = []
        for i in range(cycles):
            for j, (root_name, kind) in enumerate(chords):
                freqs = _chord(root_name, kind, octave=octave)
                cseg = tmp / "chord_{:02d}_{:02d}.wav".format(i, j)
                _build_chord_wav(freqs, chord_dur, gain=0.18, out_path=cseg)
                chord_segs.append(cseg)
                bseg = tmp / "bass_{:02d}_{:02d}.wav".format(i, j)
                _build_bass_wav(freqs[0], chord_dur, gain=0.14, out_path=bseg)
                bass_segs.append(bseg)
        chord_track = tmp / "chords.wav"
        bass_track = tmp / "bass.wav"
        _concat_wavs(chord_segs, chord_track)
        _concat_wavs(bass_segs, bass_track)

        mix_inputs = [chord_track, bass_track]

        if use_drums:
            kick_track = tmp / "kick.wav"
            _build_kick_wav(bpm, total_dur, gain=0.32, out_path=kick_track)
            mix_inputs.append(kick_track)
        if use_hihat:
            hh_track = tmp / "hh.wav"
            _build_hihat_wav(bpm, total_dur, gain=0.10, out_path=hh_track)
            mix_inputs.append(hh_track)

        out_m4a.parent.mkdir(parents=True, exist_ok=True)
        _mix_and_loudnorm(mix_inputs, out_m4a)


def _upsert_manifest_entry(manifest, entry):
    """同じ file のエントリがあれば更新、無ければ追記。"""
    for i, existing in enumerate(manifest):
        if isinstance(existing, dict) and existing.get("file") == entry["file"]:
            manifest[i] = entry
            return
    manifest.append(entry)


def generate_all():
    """12トラックを library/ に生成し manifest.json を追記/更新する。"""
    _LIBRARY.mkdir(parents=True, exist_ok=True)
    manifest = []
    if _MANIFEST.exists():
        try:
            manifest = json.loads(_MANIFEST.read_text(encoding="utf-8"))
            if not isinstance(manifest, list):
                manifest = []
        except Exception:
            manifest = []

    generated = []
    for spec in _TRACKS:
        slug, mood, bpm, _chords, _oct, _drums, _hh = spec
        out_path = _LIBRARY / "{}.m4a".format(slug)
        print("[generate] {} (mood={}, bpm={}) -> {}".format(slug, mood, bpm, out_path))
        _build_track(spec, out_path)
        entry = {
            "file": "library/{}.m4a".format(slug),
            "mood": mood,
            "license": "synthetic-starter-pack (generate_starter_pack.py で ffmpeg 合成。著作権フリー。BGM選曲エンジン検証用のスターター12曲。差し替え自由)",
            "loop_ok": True,
            "bpm": bpm,
        }
        _upsert_manifest_entry(manifest, entry)
        generated.append(entry)

    _MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("[done] {} tracks generated. manifest entries: {}".format(len(generated), len(manifest)))
    return generated


def main():
    generate_all()
    return 0


if __name__ == "__main__":
    sys.exit(main())

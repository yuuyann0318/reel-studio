# -*- coding: utf-8 -*-
"""大容量パラメトリックSFXライブラリ生成器（100〜140種）。

generate_placeholder_sfx.py（8種の固定合成）を拡張し、以下5ファミリーの
パラメータ掃引で 110 個前後の合成SFXを生成する。外部音源のダウンロードは一切
行わず、ffmpeg（anoisesrc/sine/aevalsrc + filters）のみで合成する（ライセンス
リスクゼロ）。カットごとのランダムローテーションで「効果音のパターンが少ない」
問題を解消するために作られた。

生成ファミリー:
  - whoosh (30): ノイズ + バンドパス掃引・上昇/下降
  - pop    (25): 短いサイン + 急減衰・ピッチ違い
  - riser  (20): 上昇チャープ + クレッシェンド
  - impact (20): 低域サイン + ノイズバースト
  - shimmer(15): 高域トーン群 + tremolo

品質規律:
  - 48kHz mono
  - fade in/out 5ms (クリックノイズ防止)
  - loudnorm で -14〜-20 LUFS 目安 (ファミリーごとに調整)
  - duration 0.15〜0.8s

manifest.json のエントリ形式は:
    {"file","label_jp","family","tags","duration","license"}

既存の legacy 8種 (generate_placeholder_sfx.py 由来) は
manifest 上 family="legacy" として残す (後方互換)。

使い方:
    "<repo>/.venv/bin/python3" "assets/sfx/generate_sfx_library.py"

Python 3.9 互換構文のみ。
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_FFMPEG = _HERE.parent.parent / "bin" / "ffmpeg"
_FFPROBE = _HERE.parent.parent / "bin" / "ffprobe"

_SAMPLE_RATE = 48000

_LICENSE_TEXT = (
    "synthetic-generated (generate_sfx_library.py で ffmpeg 信号合成。著作権フリー。"
    "ローテーション再生用ライブラリ。本番配布前にライセンスの明確な効果音へ差し替えてください)"
)

# 既存の legacy 8種のファイル名 (generate_placeholder_sfx.py で生成される)。
# family="legacy" として manifest に残す。
_LEGACY_FILES = [
    ("whoosh_01.wav", "ホワッシュ（場面転換）", ["transition"], 0.6),
    ("swoosh_01.wav", "スウォッシュ（動きの強調）", ["transition"], 0.5),
    ("pop_01.wav", "ポップ（選択/決定）", ["accent"], 0.3),
    ("ding_01.wav", "ディン（達成/通知）", ["accent"], 1.2),
    ("click_01.wav", "クリック（タップ/操作）", ["accent"], 0.3),
    ("riser_01.wav", "ライザー（盛り上げ）", ["riser"], 1.5),
    ("impact_01.wav", "インパクト（強調/衝撃）", ["impact"], 0.45),
    ("sparkle_01.wav", "スパークル（キラキラ演出）", ["shimmer"], 1.0),
]


def _run(cmd):
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise RuntimeError(
            "ffmpeg failed: {}\n{}".format(
                " ".join(cmd),
                proc.stderr.decode("utf-8", "replace")[-2000:],
            )
        )
    return proc


def _finalize(out_path, filter_complex, extra_inputs, loudnorm_i=-17.0):
    """filter_complexの最終ラベルを[aout]として、loudnorm+wavエンコードまで実行。

    -ac 1 (mono)、48kHz、pcm_s16le、fade in/out 5ms は各生成関数が filter 内で入れる。
    loudnorm_i でファミリーごとの音量ターゲットを変える。
    """
    filter_with_loud = filter_complex.replace(
        "[aout]",
        ",loudnorm=I={i:.1f}:TP=-1.5:LRA=7:print_format=summary[aout]".format(i=loudnorm_i),
        1,
    )
    cmd = [str(_FFMPEG), "-y"] + extra_inputs + [
        "-filter_complex", filter_with_loud,
        "-map", "[aout]",
        "-ac", "1", "-ar", str(_SAMPLE_RATE),
        "-c:a", "pcm_s16le",
        str(out_path),
    ]
    _run(cmd)


def _chirp_expr(f0, f1, duration):
    return "sin(2*PI*({f0}+({f1}-{f0})*t/{dur})*t)".format(f0=f0, f1=f1, dur=duration)


def _fade_tail(duration, fade_out_sec=0.05):
    """duration 秒のクリップに 5ms fade in / fade_out_sec fade out を掛けるフィルタ断片。"""
    fade_out_sec = min(fade_out_sec, max(0.0, duration - 0.01))
    fade_st = max(0.0, duration - fade_out_sec)
    return "afade=t=in:st=0:d=0.005,afade=t=out:st={st:.4f}:d={d:.4f}".format(
        st=fade_st, d=max(fade_out_sec, 0.001)
    )


# ---------------------------------------------------------------------------
# whoosh (30種): ノイズ + bandpass sweep
# ---------------------------------------------------------------------------

def _gen_whoosh(out_path, duration, f_low, f_high, direction, noise_color):
    """ノイズを bandpass で挟み、中心周波数を direction (up/down) で掃引する擬似 whoosh。

    ffmpeg の bandpass は静的なので、代わりに aevalsrc で chirp + noise を掛けて近似する:
      - 白/ピンクノイズを anoisesrc で作り、
      - 音量の時間関数で hf/lf のエンベロープを与える。
    """
    if direction == "down":
        expr = _chirp_expr(f_high, f_low, duration)
        vol_env = "1.0-0.6*t/{dur}".format(dur=duration)
    else:
        expr = _chirp_expr(f_low, f_high, duration)
        vol_env = "0.4+0.6*t/{dur}".format(dur=duration)

    inputs = [
        "-f", "lavfi", "-i", "aevalsrc={expr}:s={sr}:d={dur}".format(
            expr=expr, sr=_SAMPLE_RATE, dur=duration
        ),
        "-f", "lavfi", "-i", "anoisesrc=color={c}:duration={dur}:sample_rate={sr}".format(
            c=noise_color, dur=duration, sr=_SAMPLE_RATE
        ),
    ]
    filt = (
        "[0:a]volume=0.6[tone];"
        "[1:a]volume=0.5[noi];"
        "[tone][noi]amix=inputs=2:duration=longest:normalize=0[mix];"
        "[mix]volume='{env}':eval=frame,{fade}[aout]"
    ).format(env=vol_env, fade=_fade_tail(duration))
    _finalize(out_path, filt, inputs, loudnorm_i=-16.0)


def _whoosh_specs():
    specs = []
    # 5 中心周波数帯 × 3 durations × 2 direction = 30
    f_ranges = [
        (200, 900),
        (300, 1400),
        (400, 2000),
        (500, 2600),
        (700, 3400),
    ]
    durations = [0.25, 0.45, 0.7]
    colors = ["pink", "white", "brown"]
    for fi, (fl, fh) in enumerate(f_ranges):
        for di, dur in enumerate(durations):
            for direction in ("up", "down"):
                color = colors[(fi + di) % len(colors)]
                specs.append({
                    "family": "whoosh",
                    "tags": ["transition"],
                    "duration": dur,
                    "params": {
                        "f_low": fl,
                        "f_high": fh,
                        "direction": direction,
                        "noise_color": color,
                    },
                })
    return specs


# ---------------------------------------------------------------------------
# pop (25種): 短いサイン + 急減衰
# ---------------------------------------------------------------------------

def _gen_pop(out_path, duration, frequency):
    inputs = ["-f", "lavfi", "-i", "sine=frequency={f}:duration={dur}:sample_rate={sr}".format(
        f=frequency, dur=duration, sr=_SAMPLE_RATE,
    )]
    # 指数的減衰: exp(-t*k)。durationに応じてkを決める。
    decay_k = 8.0 + 24.0 / max(duration, 0.05)
    filt = (
        "[0:a]volume='exp(-t*{k:.3f})':eval=frame,{fade}[aout]"
    ).format(k=decay_k, fade=_fade_tail(duration, fade_out_sec=min(0.05, duration * 0.3)))
    _finalize(out_path, filt, inputs, loudnorm_i=-18.0)


def _pop_specs():
    specs = []
    # 5 durations × 5 freqs = 25
    durations = [0.08, 0.12, 0.18, 0.24, 0.30]
    freqs = [130, 180, 240, 320, 440]
    for dur in durations:
        for f in freqs:
            specs.append({
                "family": "pop",
                "tags": ["accent"],
                "duration": dur,
                "params": {"frequency": f},
            })
    return specs


# ---------------------------------------------------------------------------
# riser (20種): 上昇チャープ + crescendo
# ---------------------------------------------------------------------------

def _gen_riser(out_path, duration, f_low, f_high, noise_mix):
    """上昇チャープ + オプションでノイズ混合 + クレッシェンド。"""
    expr = _chirp_expr(f_low, f_high, duration)
    inputs = [
        "-f", "lavfi", "-i", "aevalsrc={expr}:s={sr}:d={dur}".format(
            expr=expr, sr=_SAMPLE_RATE, dur=duration
        ),
    ]
    if noise_mix:
        inputs += [
            "-f", "lavfi", "-i", "anoisesrc=color=pink:duration={dur}:sample_rate={sr}".format(
                dur=duration, sr=_SAMPLE_RATE
            ),
        ]
        chain = (
            "[0:a]volume=0.7[tone];"
            "[1:a]volume=0.25[noi];"
            "[tone][noi]amix=inputs=2:duration=longest:normalize=0[mix]"
        )
        base = "[mix]"
    else:
        chain = "[0:a]volume=0.9[mix]"
        base = "[mix]"
    vol_expr = "0.15+(0.9-0.15)*t/{dur}".format(dur=duration)
    filt = (
        "{chain};{base}volume='{vexpr}':eval=frame,{fade}[aout]"
    ).format(chain=chain, base=base, vexpr=vol_expr, fade=_fade_tail(duration, fade_out_sec=0.08))
    _finalize(out_path, filt, inputs, loudnorm_i=-15.0)


def _riser_specs():
    specs = []
    # 4 duration × 5 freq range = 20
    durations = [0.5, 0.6, 0.7, 0.8]
    ranges = [
        (150, 1500),
        (200, 2000),
        (250, 2600),
        (180, 3200),
        (300, 3800),
    ]
    for i, dur in enumerate(durations):
        for j, (fl, fh) in enumerate(ranges):
            specs.append({
                "family": "riser",
                "tags": ["riser"],
                "duration": dur,
                "params": {
                    "f_low": fl,
                    "f_high": fh,
                    "noise_mix": ((i + j) % 2 == 0),
                },
            })
    return specs


# ---------------------------------------------------------------------------
# impact (20種): 低域サイン + ノイズバースト
# ---------------------------------------------------------------------------

def _gen_impact(out_path, duration, frequency, noise_color):
    inputs = [
        "-f", "lavfi", "-i", "sine=frequency={f}:duration={dur}:sample_rate={sr}".format(
            f=frequency, dur=duration, sr=_SAMPLE_RATE,
        ),
        "-f", "lavfi", "-i", "anoisesrc=color={c}:duration={dur}:sample_rate={sr}".format(
            c=noise_color, dur=duration, sr=_SAMPLE_RATE,
        ),
    ]
    # ノイズは冒頭で強く鳴り、指数減衰。低域サインはやや長く残す。
    noise_decay = 10.0 + 20.0 / max(duration, 0.1)
    sine_decay = 3.0 + 6.0 / max(duration, 0.1)
    filt = (
        "[1:a]volume='0.5*exp(-t*{nk:.3f})':eval=frame[noi];"
        "[0:a]volume='0.9*exp(-t*{sk:.3f})':eval=frame[sub];"
        "[sub][noi]amix=inputs=2:duration=longest:normalize=0[mix];"
        "[mix]{fade}[aout]"
    ).format(
        nk=noise_decay, sk=sine_decay,
        fade=_fade_tail(duration, fade_out_sec=min(0.15, duration * 0.4)),
    )
    _finalize(out_path, filt, inputs, loudnorm_i=-14.0)


def _impact_specs():
    specs = []
    # 5 freq × 4 duration = 20
    freqs = [40, 55, 70, 90, 110]
    durations = [0.20, 0.30, 0.45, 0.60]
    colors = ["brown", "pink"]
    for fi, f in enumerate(freqs):
        for di, dur in enumerate(durations):
            specs.append({
                "family": "impact",
                "tags": ["impact"],
                "duration": dur,
                "params": {
                    "frequency": f,
                    "noise_color": colors[(fi + di) % len(colors)],
                },
            })
    return specs


# ---------------------------------------------------------------------------
# shimmer (15種): 高域トーン群 + tremolo
# ---------------------------------------------------------------------------

def _gen_shimmer(out_path, duration, freqs, tremolo_freq):
    inputs = []
    for f in freqs:
        inputs += ["-f", "lavfi", "-i", "sine=frequency={f}:duration={dur}:sample_rate={sr}".format(
            f=f, dur=duration, sr=_SAMPLE_RATE,
        )]
    per_gain = 0.9 / max(len(freqs), 1)
    chain_parts = []
    labels = []
    for i in range(len(freqs)):
        label = "s{}".format(i)
        chain_parts.append("[{i}:a]volume={g:.4f}[{l}]".format(i=i, g=per_gain, l=label))
        labels.append("[{}]".format(label))
    chain_parts.append(
        "{lbls}amix=inputs={n}:duration=longest:normalize=0[mix]".format(
            lbls="".join(labels), n=len(freqs)
        )
    )
    filt = "{chain};[mix]tremolo=f={tf}:d=0.6,{fade}[aout]".format(
        chain=";".join(chain_parts), tf=tremolo_freq,
        fade=_fade_tail(duration, fade_out_sec=min(0.15, duration * 0.4)),
    )
    _finalize(out_path, filt, inputs, loudnorm_i=-19.0)


def _shimmer_specs():
    specs = []
    # 5 freq stack × 3 (duration, tremolo) = 15
    stacks = [
        [1760, 2637, 3520],
        [2200, 3300, 4400],
        [1980, 2960, 3960, 4950],
        [2093, 3136, 4186],
        [1568, 2349, 3136, 4186],
    ]
    dur_trem = [(0.6, 7.0), (0.9, 9.0), (1.2, 5.0)]
    for si, freqs in enumerate(stacks):
        for di, (dur, tf) in enumerate(dur_trem):
            specs.append({
                "family": "shimmer",
                "tags": ["shimmer"],
                "duration": dur,
                "params": {"freqs": freqs, "tremolo_freq": tf},
            })
    return specs


# ---------------------------------------------------------------------------
# ディスパッチ
# ---------------------------------------------------------------------------

_FAMILY_GENERATORS = {
    "whoosh": _gen_whoosh,
    "pop": _gen_pop,
    "riser": _gen_riser,
    "impact": _gen_impact,
    "shimmer": _gen_shimmer,
}


def all_specs():
    """全ファミリー分の仕様リストを返す（generatorに渡す前・ファイル名は未確定）。"""
    return (
        _whoosh_specs()
        + _pop_specs()
        + _riser_specs()
        + _impact_specs()
        + _shimmer_specs()
    )


def _label_jp_for(family):
    return {
        "whoosh": "ホワッシュ（場面転換）",
        "pop": "ポップ（アクセント）",
        "riser": "ライザー（盛り上げ）",
        "impact": "インパクト（強調/衝撃）",
        "shimmer": "シマー（キラキラ演出）",
    }.get(family, family)


def _probe_duration(path):
    proc = subprocess.run(
        [str(_FFPROBE), "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        return round(float(proc.stdout.decode("utf-8", "replace").strip()), 3)
    except Exception:
        return None


def generate_one(spec, out_dir, index_in_family):
    """1エントリを生成し、manifestエントリ辞書を返す。"""
    family = spec["family"]
    filename = "{fam}_gen_{idx:03d}.wav".format(fam=family, idx=index_in_family)
    out_path = Path(out_dir) / filename
    gen_fn = _FAMILY_GENERATORS[family]
    gen_fn(out_path, duration=spec["duration"], **spec["params"])
    actual_duration = _probe_duration(out_path)
    return {
        "file": filename,
        "label_jp": _label_jp_for(family),
        "family": family,
        "tags": list(spec["tags"]),
        "duration": actual_duration if actual_duration is not None else spec["duration"],
        "license": _LICENSE_TEXT,
    }


def _legacy_manifest_entries():
    entries = []
    for filename, label_jp, tags, dur in _LEGACY_FILES:
        entries.append({
            "file": filename,
            "label_jp": label_jp,
            "family": "legacy",
            "tags": list(tags),
            "duration": dur,
            "license": (
                "synthetic-placeholder (generate_placeholder_sfx.py で ffmpeg 信号合成。"
                "著作権フリー。SFXオーバーレイ機能の実機検証用。本番配布前にライセンスの"
                "明確な効果音へ差し替えてください)"
            ),
        })
    return entries


def main(out_dir=None, families=None, write_manifest=True):
    """ライブラリ全体を生成し、manifest.json を書き出す。

    families: 生成対象のファミリー名リスト（Noneなら全ファミリー）。
    write_manifest: False にすると manifest.json は上書きしない
                     （テスト等で個別ファイルだけ生成したいとき用）。
    Returns: 生成した manifest エントリのリスト（legacy を含む）。
    """
    out_dir = Path(out_dir or _HERE)
    out_dir.mkdir(parents=True, exist_ok=True)

    specs = all_specs()
    if families:
        specs = [s for s in specs if s["family"] in families]

    # ファミリーごとに 001, 002, ... と番号を振る
    counters = {}
    new_entries = []
    for spec in specs:
        fam = spec["family"]
        counters[fam] = counters.get(fam, 0) + 1
        entry = generate_one(spec, out_dir, counters[fam])
        print("[generate_sfx_library] {} ({}s)".format(entry["file"], entry["duration"]))
        new_entries.append(entry)

    # upsert: 既存 manifest.json を尊重し、file をキーに (legacy + 新規生成) で上書き。
    # これにより「手動追記の family/tags/label_jp 等」を保全する（starter側と対称）。
    generated_by_file = {}
    for entry in _legacy_manifest_entries() + new_entries:
        generated_by_file[entry["file"]] = entry

    manifest_path = out_dir / "manifest.json"
    existing = []
    if manifest_path.exists():
        try:
            loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                existing = [e for e in loaded if isinstance(e, dict) and e.get("file")]
        except Exception:
            existing = []

    upserted = []
    seen_files = set()
    # まず既存エントリを順序保持で走査し、生成側にあるものは新版で上書き
    for e in existing:
        fn = e["file"]
        if fn in generated_by_file:
            upserted.append(generated_by_file[fn])
        else:
            upserted.append(e)  # 手動追記エントリを保全
        seen_files.add(fn)
    # 既存に無い新規生成エントリを末尾に追加
    for fn, entry in generated_by_file.items():
        if fn not in seen_files:
            upserted.append(entry)

    if write_manifest:
        manifest_path.write_text(
            json.dumps(upserted, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print("[generate_sfx_library] manifest -> {} (entries={})".format(
            manifest_path, len(upserted)
        ))

    return upserted


if __name__ == "__main__":
    main()

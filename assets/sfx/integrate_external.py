# -*- coding: utf-8 -*-
"""外部実物SFX(assets/sfx/external/*.mp3)を manifest.json へ統合する。

背景:
    合成SFX（*_gen_*.wav 系）だけだとカット効果音の「音色のDNA」が同じで、
    どの動画も同じ質感になる。実物SFX(効果音ラボ・商用可)を主役ローテーションに
    載せ替えるため、外部ファイルを走査し family/tags を推定して manifest へ
    upsert する。generate_sfx_library.py と対称的な upsert（手動追記や既存の
    合成SFXエントリを保全）。

使い方:
    "<repo>/.venv/bin/python3" "assets/sfx/integrate_external.py"

    または import して:
        from assets.sfx.integrate_external import integrate

分類ルール（ファイル名に含まれるキーワードで判定・上から順に評価）:
    - decision / click / cursor / button_beep / button_cancel / button_data
      → family="accent" (短いクリック/決定音)
    - swing / swish / whoosh / air-horn / brake / boyon / boyoyon
      → family="whoosh" (動きの効果音)
    - kira / shine / shimmer / sparkle / chan-chan / bell / chime / blink
      → family="shimmer" (キラキラ/決着音)
    - don / impact / hit / punch / blow / slash / bite / ban / buzzer / ax
      → family="impact" (衝撃/打撃)
    - riser / rise / warning / approach / aura / booster / buildup / curse
      → family="riser" (盛り上げ・予兆)
    - 上記いずれにも当たらない → family="texture" (環境音/演出音/長尺BGM素材)

tags:
    - family に対応する意味タグ (transition/accent/impact/riser/shimmer/ambient)
    - external 全件に "external" タグ (フィルタ用途)
    - 尺 > 2.5秒 の音源は "long" タグを付け、カットSEローテからは除外する
      (カットSEは短音のみ)

manifest エントリ形式:
    {"file": "external/<basename>", "label_jp": <basename拡張子なし>,
     "family": <推定family>, "tags": [..], "duration": <ffprobe実測>,
     "source": "external", "license": <LICENSE.md抜粋 or "external"> }

Python 3.9 互換構文のみ。
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_EXTERNAL_DIR = _HERE / "external"
_MANIFEST_PATH = _HERE / "manifest.json"
_FFPROBE = _HERE.parent.parent / "bin" / "ffprobe"
_FFMPEG = _HERE.parent.parent / "bin" / "ffmpeg"

# 尺閾値: これを超える音源は「long」タグ + カットSEローテ除外扱い。
LONG_DURATION_THRESHOLD_SEC = 2.5

# volumedetect 出力から mean_volume を抜き出す正規表現。
# 例: "[Parsed_volumedetect_0 @ 0x...] mean_volume: -24.1 dB"
_MEAN_VOLUME_RE = re.compile(r"mean_volume:\s*(-?\d+(?:\.\d+)?)\s*dB")

# ファイル名キーワード → family の判定。順に評価し最初の一致を採用する。
# 語順は「より具体的なもの」から「一般的なもの」の順に並べる（button_beep が
# 単なる "beep" より先など）。半角ハイフン・アンダースコアはどちらでも
# ヒットするようファイル名側を lowercase 化してから照合する。
_CLASSIFY_RULES = (
    # accent（短いクリック/決定音 = 主役の刻みSE候補）
    ("accent", (
        "decision", "cursor", "click", "button_beep", "button_cancel",
        "button_data", "select",
    )),
    # whoosh（動きの効果音 = 場面転換/横切り）
    ("whoosh", (
        "swing", "swish", "whoosh", "air-horn", "brake",
        "boyon", "boyoyon", "buun",
    )),
    # shimmer（キラキラ/決着音）
    ("shimmer", (
        "kira", "shine", "shimmer", "sparkle", "chan-chan",
        "bell", "chime", "blink", "correct", "cute-motion", "cute-pose",
    )),
    # impact（衝撃/打撃）
    ("impact", (
        "impact", "hit", "punch", "blow", "slash",
        "bite", "ban1", "ban2", "buzzer", "ax-", "arrow-pierce",
        "衝撃", "don1", "don2",
    )),
    # riser（盛り上げ/予兆）
    ("riser", (
        "riser", "rise", "warning", "approach", "aura",
        "booster", "buildup", "curse", "anxiety", "fear",
        "broadcasting-start",
    )),
)

_FAMILY_TAG = {
    "accent": "accent",
    "whoosh": "transition",
    "shimmer": "shimmer",
    "impact": "impact",
    "riser": "riser",
    "texture": "ambient",
}


def probe_duration(path, ffprobe_bin=None):
    """指定 mp3/wav の尺(秒)を ffprobe で返す。取得不可なら None。"""
    ffprobe_bin = str(ffprobe_bin or _FFPROBE)
    try:
        proc = subprocess.run(
            [ffprobe_bin, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
        )
        if proc.returncode != 0:
            return None
        text = proc.stdout.decode("utf-8", "replace").strip()
        if not text or text == "N/A":
            return None
        return float(text)
    except Exception:
        return None


def probe_mean_volume(path, ffmpeg_bin=None):
    """指定音源ファイルの mean_volume(dB) を ffmpeg volumedetect で返す。取得不可なら None。

    volumedetect フィルタは音声の全区間の RMS 平均を dB(FS) で報告する。
    render.py の SFX gain 補正で「target -16dB との差分」で使う。
    """
    ffmpeg_bin = str(ffmpeg_bin or _FFMPEG)
    try:
        proc = subprocess.run(
            [ffmpeg_bin, "-hide_banner", "-nostats", "-i", str(path),
             "-af", "volumedetect", "-f", "null", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
        )
        # volumedetect はエラー出力側にログを吐く。returncode が 0 でなくても
        # 音声トラックが読めれば mean_volume 行が出ることがあるため、常に stderr を走査する。
        text = proc.stderr.decode("utf-8", "replace") + "\n" + proc.stdout.decode("utf-8", "replace")
        m = _MEAN_VOLUME_RE.search(text)
        if not m:
            return None
        return float(m.group(1))
    except Exception:
        return None


def classify_family(filename_lower):
    """ファイル名(小文字化済み)から family を推定する。当てはまらなければ "texture"。"""
    for family, keywords in _CLASSIFY_RULES:
        for kw in keywords:
            if kw in filename_lower:
                return family
    return "texture"


def build_tags(family, duration_sec):
    """family と duration から tags を組み立てる。>2.5秒には "long" を付与する。"""
    tags = [_FAMILY_TAG.get(family, "ambient"), "external"]
    if duration_sec is not None and duration_sec > LONG_DURATION_THRESHOLD_SEC:
        tags.append("long")
    return tags


def _label_jp_for_stem(stem):
    """外部ファイル名から表示ラベル（label_jp）を作る。拡張子除去した stem を
    そのまま流用する（元名を辿れるよう保守的に）。"""
    return "external:{}".format(stem)


def scan_external(external_dir=None, ffprobe_bin=None, ffmpeg_bin=None):
    """external/*.mp3 を走査し、manifest エントリ (dict) の配列を返す。

    尺の取得に失敗した場合はエントリを飛ばさず duration=None のまま返し、
    tags に "long" は付けない（未知扱い＝短音ローテ対象として残す。誤検知を
    許容してもエントリ数を落とさない方針。呼び出し側でスキップしたければ
    duration is None を判定できる）。

    mean_volume_db は ffmpeg volumedetect で計測する。取得不可なら None のまま
    エントリを返す（render 側は補正なしで通す）。
    """
    external_dir = Path(external_dir or _EXTERNAL_DIR)
    if not external_dir.exists():
        return []
    entries = []
    for p in sorted(external_dir.glob("*.mp3")):
        name_lower = p.name.lower()
        family = classify_family(name_lower)
        duration = probe_duration(p, ffprobe_bin=ffprobe_bin)
        tags = build_tags(family, duration)
        mean_volume = probe_mean_volume(p, ffmpeg_bin=ffmpeg_bin)
        entries.append({
            "file": "external/{}".format(p.name),
            "label_jp": _label_jp_for_stem(p.stem),
            "family": family,
            "tags": tags,
            "duration": round(duration, 3) if duration is not None else None,
            "mean_volume_db": round(mean_volume, 2) if mean_volume is not None else None,
            "source": "external",
            "license": "external (assets/sfx/external/LICENSE.md 参照。効果音ラボ・商用可)",
        })
    return entries


def upsert_manifest(new_entries, manifest_path=None):
    """既存 manifest.json を尊重しつつ new_entries を upsert する
    （generate_sfx_library.py と同じ upsert パターン）。

    file をキーに、既存エントリの並び順を保持したまま同 file の内容だけを
    差し替え、既存に無いものは末尾へ追加する。手動追記や合成SFXエントリは
    保全される。

    Returns: 書き出し後の manifest 全体（list）。
    """
    manifest_path = Path(manifest_path or _MANIFEST_PATH)
    generated_by_file = {e["file"]: e for e in new_entries if e.get("file")}
    existing = []
    if manifest_path.exists():
        try:
            loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                existing = [e for e in loaded if isinstance(e, dict) and e.get("file")]
        except Exception:
            existing = []

    upserted = []
    seen = set()
    for e in existing:
        fn = e.get("file")
        if fn in generated_by_file:
            upserted.append(generated_by_file[fn])
        else:
            upserted.append(e)
        seen.add(fn)
    for fn, entry in generated_by_file.items():
        if fn not in seen:
            upserted.append(entry)

    manifest_path.write_text(
        json.dumps(upserted, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return upserted


def integrate(external_dir=None, manifest_path=None, ffprobe_bin=None, ffmpeg_bin=None):
    """外部SFXを走査 → manifest へ upsert して、結果 dict を返す。

    Returns: {"scanned": int, "upserted": int, "families": {family: count},
             "long_count": int, "manifest": <manifest list>}
    """
    new_entries = scan_external(
        external_dir=external_dir, ffprobe_bin=ffprobe_bin, ffmpeg_bin=ffmpeg_bin,
    )
    manifest = upsert_manifest(new_entries, manifest_path=manifest_path)
    families = {}
    long_count = 0
    for e in new_entries:
        families[e["family"]] = families.get(e["family"], 0) + 1
        if "long" in (e.get("tags") or []):
            long_count += 1
    return {
        "scanned": len(new_entries),
        "upserted": len(new_entries),
        "families": families,
        "long_count": long_count,
        "manifest": manifest,
    }


def main():
    result = integrate()
    print("[integrate_external] scanned={} families={} long={}".format(
        result["scanned"], result["families"], result["long_count"],
    ))
    print("[integrate_external] manifest entries total = {}".format(
        len(result["manifest"]),
    ))
    return 0


if __name__ == "__main__":
    sys.exit(main())

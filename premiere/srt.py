# -*- coding: utf-8 -*-
"""SRT字幕生成（Premiere Pro読み込み用）。

studio/server/jobs.py の _render_project および pipeline.subtitles.build_telop_pieces_from_shots
と同じタイミング契約に合わせる: plan.shots のうち enabled なものを order 順に並べ、
trim後の実尺（trim.end - trim.start）で累積してタイムラインの表示区間(out_start/out_end)を
決める（= _render_project がトリム済みクリップをその順序・その尺で連結する前提と一致）。

タイムラインの累積は premiere.export_xmeml.frame_align_durations() を使う
（「フレーム丸め(round)→フレーム基準で累積→秒へ戻す」契約。reel.xml(V1トラック)と
同じフレーム量子化で計算するため、浮動小数の秒をそのまま累積する場合に生じる
xmemlとのタイミングずれが起きない）。trim後の実尺が1フレーム未満（round後0フレーム）の
ショットはSRTエントリを出力しない（ゼロ長ショットガード。xmeml側のV1トラックスキップと
同じ契約）。

2行整形は pipeline.subtitles.wrap_caption_kinsoku をそのまま利用する（pipeline/ は編集禁止だが
参照・利用は可）。captionが空（strip後空文字）のショットはSRTエントリを出力しない。

Python 3.9 互換構文のみ。
"""
from __future__ import annotations

from pipeline.subtitles import wrap_caption_kinsoku
from premiere.export_xmeml import DEFAULT_FPS, frame_align_durations

SRT_MAX_CHARS = 13
SRT_MAX_LINES = 2


def _seconds_to_srt_timecode(t):
    """秒数を SRT の `HH:MM:SS,mmm` 形式へ変換する。"""
    if t is None or t < 0:
        t = 0.0
    total_ms = int(round(float(t) * 1000))
    ms = total_ms % 1000
    total_s = total_ms // 1000
    s = total_s % 60
    total_m = total_s // 60
    m = total_m % 60
    h = total_m // 60
    return "{:02d}:{:02d}:{:02d},{:03d}".format(h, m, s, ms)


def _enabled_shots_in_order(plan):
    shots = (plan or {}).get("shots") or []
    return sorted(
        [s for s in shots if isinstance(s, dict) and s.get("enabled", True)],
        key=lambda s: s.get("order", 0),
    )


def build_srt(plan, fps=None):
    """Studio plan（shots/order/enabled/trim/caption）からSRT字幕全文を生成する。

    Args:
        plan: Studio plan dict（studio/server/projects.py の正規化済みスキーマ想定）。
        fps: フレームレート（省略時は premiere.export_xmeml.DEFAULT_FPS=30）。
             premiere.package.build_package は reel.xml(export_xmeml.build_xmeml)と
             同じ既定値を使うため、通常は省略でよい（reel.xmlのタイムベースを変える
             場合のみ、呼び出し側で同じfpsを両方に渡すこと）。
             整数へ丸める: xmemlのtimebaseは整数フレームレートしか表現できず
             （export_xmeml.build_xmeml も同様に int(fps or DEFAULT_FPS) で整数化する。
             このコードベースはNTSC=FALSE固定でドロップフレーム端数fpsを扱わない）、
             ここでも同じ丸めを行うことで、29.97のような小数を渡された場合でも
             xmeml側（29へ切り捨て）とタイミングが食い違わないようにする
             （独立レビューで指摘されたfps不整合バグの修正）。

    Returns:
        str: UTF-8前提のSRTテキスト（改行は\\n、末尾は改行1つで終わる。エントリが
             1つも無ければ空文字列を返す）。
    """
    fps = int(fps or DEFAULT_FPS)
    shots = _enabled_shots_in_order(plan)

    durations_sec = []
    for shot in shots:
        trim = shot.get("trim") or {}
        trim_start = float(trim.get("start", 0.0))
        trim_end = float(trim.get("end", trim_start))
        durations_sec.append(max(0.0, trim_end - trim_start))
    aligned = frame_align_durations(durations_sec, fps)

    entries = []
    for shot, timing in zip(shots, aligned):
        if timing["dur_frames"] <= 0:
            continue  # ゼロ長ショットガード（1フレーム未満の実尺はSRTエントリを出力しない）
        caption = (shot.get("caption") or "").strip()
        if not caption:
            continue
        lines = wrap_caption_kinsoku(caption, max_chars=SRT_MAX_CHARS, max_lines=SRT_MAX_LINES)
        if not lines:
            continue
        entries.append((timing["start_sec"], timing["end_sec"], lines))

    if not entries:
        return ""

    blocks = []
    for index, (out_start, out_end, lines) in enumerate(entries, start=1):
        blocks.append(
            "{index}\n{start} --> {end}\n{text}\n".format(
                index=index,
                start=_seconds_to_srt_timecode(out_start),
                end=_seconds_to_srt_timecode(out_end),
                text="\n".join(lines),
            )
        )
    return "\n".join(blocks) + "\n"

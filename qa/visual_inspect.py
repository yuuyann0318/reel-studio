# -*- coding: utf-8 -*-
"""完成mp4のフレーム抽出 + Claude vision による目視相当検査。

各テロップの計画絶対秒 + 0.3s の位置でフレームを抽出し、call_claude_vision_json
で「計画された caption_jp が実際に表示されているか / 画面要素と重なっていないか /
見切れていないか」を JSON 判定させる。

CLI:
  python -m qa.visual_inspect <run_work_dir> [--max-calls N]
  python -m qa.visual_inspect --plan <plan.json> --video <output.mp4> [--max-calls N]

Python 3.9 互換構文のみ。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from typing import Any, Dict, List, Optional, Tuple

try:
    from pipeline.claude_runner import call_claude_vision_json
except Exception:  # pragma: no cover
    call_claude_vision_json = None


_DEFAULT_SAMPLE_OFFSET_SEC = 0.3
_DEFAULT_MAX_CALLS = 10  # コストガード（1 call = 1バッチ = 最大 N frame）
_DEFAULT_BATCH_SIZE = 4


# ---------------------------------------------------------------------------
# 計画抽出
# ---------------------------------------------------------------------------

def resolve_caption_sample_seconds(
    plan: Dict[str, Any],
    sample_offset_sec: float = _DEFAULT_SAMPLE_OFFSET_SEC,
    actual_duration_sec: Optional[float] = None,
    shot_display_durations: Optional[Dict[str, float]] = None,
) -> List[Dict[str, Any]]:
    """plan.shots から「テロップ検査すべきサンプル秒」の一覧を作る（純関数）。

    caption_in_offset_sec が定義されていればそれ+offsetを、無ければ shot 中央+offsetを
    サンプル秒とする。caption_jp が空のショットはスキップ。

    shot_display_durations（{shot_id: display_dur}）を渡すと、各ショットの表示尺を
    その値で置き換える。これは run.py の音声主導タイミング同期モードでの scale と一致し、
    ショットごとの伸縮率が異なるケース(短い narration は伸びる／長いのは縮む)を正確に再現する。
    渡さない場合は従来どおり、actual_duration_sec との比で全 sample_sec を一律スケールする。

    Returns: [{"shot_id","caption_jp","sample_sec"}, ...]  sample_sec 昇順。
    """
    shots = plan.get("shots") or []
    plan_total = sum(float(s.get("duration_sec") or 0.0) for s in shots if isinstance(s, dict))
    global_scale = 1.0
    if actual_duration_sec is not None and plan_total > 0:
        global_scale = float(actual_duration_sec) / float(plan_total)
    cursor = 0.0
    samples: List[Dict[str, Any]] = []
    for s in shots:
        if not isinstance(s, dict):
            continue
        plan_dur = float(s.get("duration_sec") or 0.0)
        sid = s.get("id")
        # 表示尺を per-shot で解決(あれば shot_display_durations を使う)
        if shot_display_durations is not None and sid in shot_display_durations:
            display_dur = float(shot_display_durations[sid])
        else:
            display_dur = plan_dur * global_scale
        per_shot_scale = (display_dur / plan_dur) if plan_dur > 0 else 1.0
        caption = (s.get("caption_jp") or "").strip()
        cin = s.get("caption_in_offset_sec")
        cout = s.get("caption_out_offset_sec")
        if not caption:
            cursor += display_dur
            continue
        # caption の中央付近を取る（in が定義されていれば in+offset、無ければ shot 中央）。
        # cin/cout は plan 尺基準なので per-shot scale で補正する。
        if isinstance(cin, (int, float)):
            local_off = (float(cin) + float(sample_offset_sec)) * per_shot_scale
            if isinstance(cout, (int, float)):
                local_off = min(local_off, float(cout) * per_shot_scale - 1e-3)
                local_off = max(local_off, float(cin) * per_shot_scale + 1e-3)
        else:
            local_off = min(display_dur - 0.1, display_dur * 0.5 + float(sample_offset_sec))
        local_off = max(0.0, min(local_off, display_dur - 1e-3))
        raw_sec = cursor + local_off
        samples.append({
            "shot_id": sid,
            "caption_jp": caption,
            "sample_sec": round(raw_sec, 3),
        })
        cursor += display_dur
    samples.sort(key=lambda x: x["sample_sec"])
    return samples


def _probe_actual_duration_sec(ffmpeg_bin: str, video_path: str) -> Optional[float]:
    """ffmpeg -i で total duration を秒で取り出す（ffprobeが無い前提でも動くよう ffmpeg 経由）。"""
    if not video_path or not os.path.isfile(video_path):
        return None
    # 同梱の ffprobe があるならそれを使う（呼び出し側で cfg から取得できるが、
    # ここは軽量な ffmpeg -i の stderr パースで済ませる）。
    try:
        proc = subprocess.run([ffmpeg_bin, "-i", video_path], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stderr = proc.stderr.decode("utf-8", "replace")
    except Exception:
        return None
    import re as _re
    m = _re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", stderr)
    if not m:
        return None
    h, mn, se = m.group(1), m.group(2), m.group(3)
    try:
        return int(h) * 3600 + int(mn) * 60 + float(se)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# フレーム抽出
# ---------------------------------------------------------------------------

def extract_frames_at_times(
    ffmpeg_bin: str,
    video_path: str,
    times_sec: List[float],
    out_dir: str,
) -> List[str]:
    """各時刻で 1 フレームを PNG に書き出し、書き出せたパスの一覧を返す。

    ffmpeg -ss <t> -i <video> -frames:v 1 <out.png>。既存の
    pipeline/reference_v2.extract_frames_at_times と同じ手口。
    """
    os.makedirs(out_dir, exist_ok=True)
    paths: List[str] = []
    for i, t in enumerate(times_sec):
        out_path = os.path.join(out_dir, "frame_{:03d}_{:07.3f}.png".format(i, t))
        cmd = [
            ffmpeg_bin, "-y", "-ss", "{:.3f}".format(max(0.0, float(t))),
            "-i", video_path,
            "-frames:v", "1", "-q:v", "2", out_path,
        ]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if proc.returncode == 0 and os.path.isfile(out_path):
            paths.append(out_path)
    return paths


# ---------------------------------------------------------------------------
# vision 判定
# ---------------------------------------------------------------------------

_VISION_PROMPT_TEMPLATE = """あなたはリール動画の目視QA担当です。次の各PNGは、完成した縦型9:16リール
(1080x1920) から特定の秒数で切り出したフレームです。それぞれのフレームについて、
以下の項目を判定し、JSON配列で返してください。

判定項目:
  - "caption_visible": 期待するテロップ文言が画面に見えているか（true/false）
  - "caption_readable": テロップが読みやすいか（他要素と重ならず、見切れず、
    十分なコントラストがあるか）（true/false）
  - "issues": 問題点の短い日本語配列（無ければ []）

入力フレーム(順序どおり):
{FRAMES_BLOCK}

出力形式（各フレームに 1 要素の配列。順序は入力順）:
[
  {{"frame_index": 0, "shot_id": "s1", "caption_visible": true, "caption_readable": true, "issues": []}},
  ...
]

コードブロック無しで JSON 配列だけを返してください。"""


def _build_frames_block(items: List[Dict[str, Any]]) -> str:
    lines = []
    for i, it in enumerate(items):
        lines.append(
            "  frame_index={idx}: shot_id={sid}, sample_sec={t}, expected_caption_jp={cap!r}, path={p}".format(
                idx=i, sid=it.get("shot_id"), t=it.get("sample_sec"),
                cap=it.get("caption_jp"), p=it.get("frame_path"),
            )
        )
    return "\n".join(lines)


def inspect_frames_batch(
    items: List[Dict[str, Any]],
    timeout_sec: int = 600,
) -> Dict[str, Any]:
    """1 バッチ (<=N frame) を Claude vision へ投げて判定結果を得る。

    items: [{"shot_id","caption_jp","sample_sec","frame_path"}, ...]
    """
    if not items:
        return {"ok": True, "verdicts": []}
    if call_claude_vision_json is None:
        return {"ok": False, "error": "call_claude_vision_json 利用不可", "verdicts": []}
    prompt = _VISION_PROMPT_TEMPLATE.replace("{FRAMES_BLOCK}", _build_frames_block(items))
    paths = [it["frame_path"] for it in items if it.get("frame_path")]
    result = call_claude_vision_json(prompt, paths, timeout_sec=timeout_sec)
    if not result.get("ok"):
        return {"ok": False, "error": result.get("error"), "verdicts": []}
    data = result.get("data")
    if isinstance(data, dict):
        # 単発オブジェクトで返る場合の吸収
        data = [data]
    if not isinstance(data, list):
        return {"ok": False, "error": "vision 応答が配列/オブジェクトではありません", "verdicts": []}
    # frame_index を items の shot_id と紐付ける
    verdicts: List[Dict[str, Any]] = []
    for i, v in enumerate(data):
        if not isinstance(v, dict):
            continue
        idx = v.get("frame_index")
        if not isinstance(idx, int) or not (0 <= idx < len(items)):
            idx = i if i < len(items) else None
        item = items[idx] if idx is not None else {}
        verdicts.append({
            "shot_id": v.get("shot_id") or (item.get("shot_id") if item else None),
            "sample_sec": item.get("sample_sec") if item else None,
            "expected_caption_jp": item.get("caption_jp") if item else None,
            "caption_visible": bool(v.get("caption_visible")),
            "caption_readable": bool(v.get("caption_readable")),
            "issues": [str(x) for x in (v.get("issues") or [])],
        })
    return {"ok": True, "verdicts": verdicts, "model_used": result.get("model_used")}


def run_visual_inspect(
    plan: Dict[str, Any],
    video_path: str,
    ffmpeg_bin: str,
    max_calls: int = _DEFAULT_MAX_CALLS,
    batch_size: int = _DEFAULT_BATCH_SIZE,
    sample_offset_sec: float = _DEFAULT_SAMPLE_OFFSET_SEC,
    work_dir: Optional[str] = None,
    timeout_sec: int = 600,
    shot_display_durations: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    """plan と完成mp4 からテロップの目視相当検査を実行する。

    Returns: {"ok": bool, "overall_ok": bool, "verdicts": [...], "meta": {...}}
    """
    actual_duration = _probe_actual_duration_sec(ffmpeg_bin, video_path)
    samples = resolve_caption_sample_seconds(
        plan, sample_offset_sec=sample_offset_sec, actual_duration_sec=actual_duration,
        shot_display_durations=shot_display_durations,
    )
    if not samples:
        return {"ok": True, "overall_ok": True, "verdicts": [], "meta": {"reason": "検査対象キャプション無し"}}

    # コストガード: max_calls * batch_size を超える場合は前方から間引く（sample_sec 昇順の均等間引き）
    max_items = int(max_calls) * int(batch_size)
    if max_items > 0 and len(samples) > max_items:
        step = len(samples) / float(max_items)
        picked = []
        acc = 0.0
        for i in range(max_items):
            idx = int(acc)
            if idx >= len(samples):
                idx = len(samples) - 1
            picked.append(samples[idx])
            acc += step
        samples = picked

    tmp = work_dir or tempfile.mkdtemp(prefix="visual_inspect_")
    try:
        frame_paths = extract_frames_at_times(
            ffmpeg_bin, video_path, [s["sample_sec"] for s in samples], tmp,
        )
        # フレーム抽出できたものだけを対象に
        items: List[Dict[str, Any]] = []
        for s, p in zip(samples, frame_paths):
            items.append({
                "shot_id": s["shot_id"],
                "caption_jp": s["caption_jp"],
                "sample_sec": s["sample_sec"],
                "frame_path": p,
            })

        all_verdicts: List[Dict[str, Any]] = []
        errors: List[str] = []
        call_count = 0
        for start in range(0, len(items), batch_size):
            if call_count >= max_calls:
                break
            batch = items[start:start + batch_size]
            res = inspect_frames_batch(batch, timeout_sec=timeout_sec)
            call_count += 1
            if not res.get("ok"):
                errors.append(res.get("error") or "vision call 失敗")
                continue
            all_verdicts.extend(res.get("verdicts") or [])

        overall_ok = bool(all_verdicts) and all(
            v.get("caption_visible") and v.get("caption_readable") for v in all_verdicts
        )
        return {
            "ok": len(errors) == 0,
            "overall_ok": overall_ok,
            "verdicts": all_verdicts,
            "errors": errors,
            "meta": {
                "sample_count": len(items),
                "call_count": call_count,
                "max_calls": max_calls,
                "batch_size": batch_size,
            },
        }
    finally:
        # デバッグしやすいよう work_dir=指定時は残す。tempfile 側は残置（呼び出し側で掃除）。
        pass


def _load_json(path: str):
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _find_output_mp4_for_run(run_dir: str) -> Optional[str]:
    if not run_dir:
        return None
    name = os.path.basename(os.path.normpath(run_dir))
    project = os.path.dirname(os.path.dirname(os.path.abspath(run_dir)))
    candidate = os.path.join(project, "output", "{}.mp4".format(name))
    return candidate if os.path.isfile(candidate) else None


def _probe_shot_display_durations(run_dir: str, plan: Dict[str, Any], ffmpeg_bin: str) -> Optional[Dict[str, float]]:
    """work/<run_id>/narration_segments/seg_XXX.wav の実尺から shot_display_durations を復元する。

    run.py の音声主導タイミング同期モードと同じ計算式:
      display_dur = max(seg_duration + SYNC_GAP_SEC, SYNC_MIN_SHOT_SEC)
    プロジェクト依存の import を最小化するため、定数はここで直接指定する
    (pipeline.render の SYNC_GAP_SEC=0.25 / SYNC_MIN_SHOT_SEC=1.2 に対応)。
    """
    if not run_dir:
        return None
    seg_dir = os.path.join(run_dir, "narration_segments")
    if not os.path.isdir(seg_dir):
        return None
    shots = plan.get("shots") or []
    if not shots:
        return None
    seg_paths = sorted(
        os.path.join(seg_dir, name) for name in os.listdir(seg_dir)
        if name.startswith("seg_") and name.endswith(".wav")
    )
    if len(seg_paths) < len(shots):
        return None
    SYNC_GAP_SEC = 0.25
    SYNC_MIN_SHOT_SEC = 1.2
    displays: Dict[str, float] = {}
    for shot, seg_path in zip(shots, seg_paths):
        try:
            proc = subprocess.run(
                [ffmpeg_bin, "-i", seg_path], stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            stderr = proc.stderr.decode("utf-8", "replace")
            import re as _re
            m = _re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", stderr)
            if not m:
                continue
            seg_dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
        except Exception:
            continue
        sid = shot.get("id")
        if not sid:
            continue
        displays[sid] = max(seg_dur + SYNC_GAP_SEC, SYNC_MIN_SHOT_SEC)
    return displays if displays else None


def main(argv=None):  # pragma: no cover - CLI
    parser = argparse.ArgumentParser(
        description="higgsfield-auto-reel テロップ目視相当検査（Claude vision）"
    )
    parser.add_argument("run_dir", nargs="?", help="work/<run_id>/ を渡すと plan.json / output mp4 を自動発見")
    parser.add_argument("--plan", default=None)
    parser.add_argument("--video", default=None)
    parser.add_argument("--max-calls", type=int, default=_DEFAULT_MAX_CALLS,
                         help="Claude vision の最大呼び出し回数（コストガード）")
    parser.add_argument("--batch-size", type=int, default=_DEFAULT_BATCH_SIZE)
    parser.add_argument("--work-dir", default=None, help="フレーム保存先（指定時は残す）")
    args = parser.parse_args(argv)

    plan_path = args.plan
    video_path = args.video
    if args.run_dir:
        run_dir = args.run_dir
        if plan_path is None:
            candidate = os.path.join(run_dir, "plan.json")
            plan_path = candidate if os.path.isfile(candidate) else None
        if video_path is None:
            video_path = _find_output_mp4_for_run(run_dir)
    else:
        run_dir = None

    plan = _load_json(plan_path) if plan_path else None
    if plan is None:
        print("[visual_inspect] error: plan.json が読めません", file=sys.stderr)
        return 2
    if not video_path or not os.path.isfile(video_path):
        print("[visual_inspect] error: 対象mp4 が見つかりません", file=sys.stderr)
        return 2

    from pipeline.config import load_config
    cfg = load_config()
    ffmpeg_bin = cfg.get("ffmpeg_bin")

    # 音声主導タイミング同期モードでは各ショットの表示尺が narration_segments に依存する。
    # narration_segments が work_dir に残っていれば、そこから per-shot display_duration を
    # 復元してサンプル秒を正確に補正する(そうしないと BUG-55 で修正済みの scale と
    # 齟齬が起きてサンプルが取り違えのショットに落ちる)。
    shot_display_durations = None
    if args.run_dir:
        shot_display_durations = _probe_shot_display_durations(args.run_dir, plan, ffmpeg_bin)

    report = run_visual_inspect(
        plan=plan, video_path=video_path, ffmpeg_bin=ffmpeg_bin,
        max_calls=args.max_calls, batch_size=args.batch_size, work_dir=args.work_dir,
        shot_display_durations=shot_display_durations,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("overall_ok") else 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

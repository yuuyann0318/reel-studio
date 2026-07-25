# -*- coding: utf-8 -*-
"""②検知（QA層・読み取りのみ）: 生成クリップ内の「文字化け（AIが描いた偽文字）」検知。

ユーザー報告: Higgsfield実映像の中に、AIが描いてしまった偽文字・崩れた日本語風の
文字がちょくちょく出る。設計方針②検知: 生成済みクリップ（テロップ焼き込み【前】）から
代表フレームを抽出し、Claude vision で「映像内に文字/文字状のものが見えるか・可読か・
化けているか」を判定して **報告のみ** 行う（作り直しはユーザー判断）。

★検査対象は必ずテロップ焼き込み前のクリップ（run.py 経路の run_dir/clips/<id>.raw.mp4、
Studio 経路の projects/<id>/clips/<id>.mp4）にする。焼き込み後だと、こちらが意図して
乗せた正規テロップと、映像内にAIが描いた偽文字とを区別できないため。

副作用は「フレーム抽出（ffmpeg）」「vision 呼び出し（claude CLI・読み取り専用）」のみで、
Higgsfield クレジットは一切消費しない。vision 呼び出しは config の上限
（visual.text_check.max_vision_calls / vision_batch_size）でキャップする。

Python 3.9 互換構文のみ。
"""
from __future__ import annotations

import os
import tempfile
from typing import Any, Dict, List, Optional

try:  # 実行環境により claude CLI が使えない場合もあるため import は防御的に。
    from pipeline.claude_runner import call_claude_vision_json
except Exception:  # pragma: no cover
    call_claude_vision_json = None  # type: ignore

# extract_frames_at_times は visual_inspect の実装を再利用（ffmpeg -ss -frames:v 1）。
from qa.visual_inspect import extract_frames_at_times


_DEFAULT_MAX_CALLS = 3
_DEFAULT_BATCH_SIZE = 10
_DEFAULT_TIMEOUT_SEC = 600
# 各クリップから抽出する代表フレーム枚数（既定3枚: 先頭・中央・終盤付近）。
# 先頭/中央の2枚だけでは、カメラ移動で後から現れる看板/UI/商品ラベル等の文字を
# 見逃すため（codex 指摘）、採用区間へ等間隔でサンプリングして被覆を上げる。
_DEFAULT_FRAMES_PER_CLIP = 3

# 採用区間の端に取る内側マージン（秒）。端フレームの黒/切替を避ける。
_HEAD_SAMPLE_SEC = 0.2


_VISION_PROMPT_TEMPLATE = """あなたは縦型ショート動画（9:16）の映像QA担当です。
次の各PNGは、テロップ（字幕）を焼き込む【前】の生成クリップから切り出した代表フレームです。
この段階では画面に文字は一切無いのが正常です。ところが映像生成AIは、看板・商品パッケージ・
UI・ロゴ・透かし・崩れた日本語風/英語風の記号などの「偽の文字」を勝手に描いてしまうことが
あります。それを見つけるのがあなたの仕事です。

各フレームについて次を判定してください（こちらが後で乗せる正規のテロップは【まだ無い】前提です）:
  - "has_text": 映像の中に文字または文字状のもの（看板/パッケージ/UI/ロゴ/透かし/崩れた
    文字風の記号を含む）が見えるか（true/false）
  - "garbled": その文字が崩れている・実在しない偽物・意味をなさない文字化けか（true/false）。
    has_text=false のときは false。
  - "readable": その文字が意味の通る実在の語として判読できるか（true/false）。
    has_text=false のときは false。
  - "note": 何がどこに見えるかの短い日本語（無ければ ""）

入力フレーム（順序どおり）:
{FRAMES_BLOCK}

出力形式（各フレームに1要素の配列。順序は入力順）:
[
  {{"frame_index": 0, "shot_id": "s1", "has_text": false, "garbled": false, "readable": false, "note": ""}},
  ...
]

コードブロック無しで JSON 配列だけを返してください。"""


def _text_check_cfg(cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """config から visual.text_check 設定を取り出す（欠損は既定で補完）。"""
    visual = (cfg or {}).get("visual") if isinstance(cfg, dict) else None
    tc = (visual or {}).get("text_check") if isinstance(visual, dict) else None
    tc = tc if isinstance(tc, dict) else {}
    return {
        "enabled": bool(tc.get("enabled", True)),
        "max_vision_calls": int(tc.get("max_vision_calls", _DEFAULT_MAX_CALLS)),
        "vision_batch_size": int(tc.get("vision_batch_size", _DEFAULT_BATCH_SIZE)),
        "vision_timeout_sec": int(tc.get("vision_timeout_sec", _DEFAULT_TIMEOUT_SEC)),
        "frames_per_clip": max(1, int(tc.get("frames_per_clip", _DEFAULT_FRAMES_PER_CLIP))),
    }


def _sample_times_for_clip(
    duration_sec: Optional[float],
    start_sec: float = 0.0,
    num_frames: int = _DEFAULT_FRAMES_PER_CLIP,
) -> List[float]:
    """クリップから抽出する代表フレームの絶対時刻（秒）を返す。

    採用区間 [start_sec, start_sec+duration] を内側マージン込みで等間隔サンプリングする。
    Studio のトリム編集で start_sec>0 のショットでも「実際に使われる区間」を検査するため、
    抽出時刻は必ず start_sec を加味した絶対秒で返す（codex 指摘: トリム位置の無視を防ぐ）。
    尺が不明/0以下のときは start 近傍の1枚のみ。
    """
    try:
        d = float(duration_sec) if duration_sec is not None else 0.0
    except (TypeError, ValueError):
        d = 0.0
    try:
        s = max(0.0, float(start_sec))
    except (TypeError, ValueError):
        s = 0.0
    if d <= 0.0:
        return [round(s + _HEAD_SAMPLE_SEC, 3)]
    n = max(1, int(num_frames))
    margin = min(_HEAD_SAMPLE_SEC, d * 0.1)
    lo = s + margin
    hi = s + d - margin
    if hi <= lo:
        return [round(s + d / 2.0, 3)]
    if n == 1:
        return [round((lo + hi) / 2.0, 3)]
    step = (hi - lo) / (n - 1)
    return [round(lo + step * i, 3) for i in range(n)]


def _build_frames_block(items: List[Dict[str, Any]]) -> str:
    lines = []
    for i, it in enumerate(items):
        lines.append(
            "  frame_index={idx}: shot_id={sid}, sample_sec={t}, path={p}".format(
                idx=i, sid=it.get("shot_id"), t=it.get("sample_sec"), p=it.get("frame_path"),
            )
        )
    return "\n".join(lines)


def inspect_frames_batch(
    items: List[Dict[str, Any]],
    timeout_sec: int = _DEFAULT_TIMEOUT_SEC,
    _vision_fn=None,
) -> Dict[str, Any]:
    """1バッチ（<=N frame）を Claude vision へ投げて frame 単位の判定を得る。

    items: [{"shot_id","sample_sec","frame_path"}, ...]
    _vision_fn: テスト用の差し替え口（未指定なら call_claude_vision_json）。
    """
    if not items:
        return {"ok": True, "verdicts": []}
    vision_fn = _vision_fn or call_claude_vision_json
    if vision_fn is None:
        return {"ok": False, "error": "call_claude_vision_json 利用不可", "verdicts": []}
    prompt = _VISION_PROMPT_TEMPLATE.replace("{FRAMES_BLOCK}", _build_frames_block(items))
    paths = [it["frame_path"] for it in items if it.get("frame_path")]
    result = vision_fn(prompt, paths, timeout_sec=timeout_sec)
    if not result.get("ok"):
        return {"ok": False, "error": result.get("error"), "verdicts": [], "complete": False}
    data = result.get("data")
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return {"ok": False, "error": "vision 応答が配列/オブジェクトではありません",
                "verdicts": [], "complete": False}
    # ★frame_index を「入力への強制対応」に使う（codex 指摘）。モデルが返す shot_id は
    # 幻覚/取り違えの恐れがあるため信用せず、必ず入力 item の shot_id を正とする。
    by_index: Dict[int, Dict[str, Any]] = {}
    for v in data:
        if not isinstance(v, dict):
            continue
        idx = v.get("frame_index")
        if isinstance(idx, int) and 0 <= idx < len(items) and idx not in by_index:
            by_index[idx] = v
    verdicts: List[Dict[str, Any]] = []
    for i, item in enumerate(items):
        v = by_index.get(i)
        if v is None:
            # この入力フレームに対応する判定が欠落（応答不足）。clean と誤断しないよう飛ばす。
            continue
        has_text = bool(v.get("has_text"))
        verdicts.append({
            "shot_id": item.get("shot_id"),   # 入力を正とする
            "sample_sec": item.get("sample_sec"),
            "has_text": has_text,
            # has_text=false のとき garbled/readable は false に正規化する。
            "garbled": bool(v.get("garbled")) and has_text,
            "readable": bool(v.get("readable")) and has_text,
            "note": str(v.get("note") or ""),
        })
    # 全入力フレーム分の判定が揃ったか（欠落があれば complete=False）。
    complete = len(verdicts) == len(items)
    return {"ok": True, "verdicts": verdicts, "complete": complete, "model_used": result.get("model_used")}


def _aggregate_shot_verdicts(shot_id: str, frame_verdicts: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """1ショットの複数フレーム判定を集約し、文字が検出された場合だけ artifact を返す。

    verdict:
      - "garbled": 文字が見え、かつ崩れている/偽物（＝最も注意すべき文字化け）
      - "text": 文字は見えるが崩れていない（意図しない実在文字。ロゴ/看板等）
    どのフレームにも文字が見えなければ None（=クリーン、記録しない）。
    """
    with_text = [v for v in frame_verdicts if v.get("has_text")]
    if not with_text:
        return None
    any_garbled = any(v.get("garbled") for v in with_text)
    verdict = "garbled" if any_garbled else "text"
    notes = [v.get("note") for v in with_text if v.get("note")]
    # 代表 note を1つ（garbled 優先）。
    note = ""
    for v in with_text:
        if v.get("garbled") and v.get("note"):
            note = v["note"]
            break
    if not note and notes:
        note = notes[0]
    return {"shot_id": shot_id, "verdict": verdict, "note": note}


def run_clip_text_check(
    clips: List[Dict[str, Any]],
    ffmpeg_bin: str,
    cfg: Optional[Dict[str, Any]] = None,
    tmp_dir: Optional[str] = None,
    _vision_fn=None,
) -> Dict[str, Any]:
    """複数クリップ（テロップ焼き込み前）を検査し text_artifacts を返す（読み取りのみ）。

    clips: [{"shot_id", "clip_path"(絶対), "duration_sec"(任意), "start_sec"(任意=トリム開始)}, ...]
    Returns:
      {
        "ok": bool,                    # 少なくとも1ショットを検査できたか
        "enabled": bool,
        "text_artifacts": [{"shot_id","verdict","note"}, ...],  # 文字検出ショットのみ
        "checked_shot_ids": [...],     # 全抽出フレームの判定が揃ったショット（=クリーン判定を出せる）
        "unchecked_shot_ids": [...],   # 呼び出し上限/応答欠落/失敗で判定が揃わなかったショット
        "calls_used": int,
        "model_used": str|None,
        "error": str|None,
      }

    ★重要（codex 指摘対応）: 呼び出し上限や vision 応答欠落で判定が揃わなかったショットは
    checked_shot_ids に入れず unchecked_shot_ids に回す。利用側が「未検査」を「クリーン」と
    誤認しないようにするため（text_artifacts は checked のショットからのみ生成する）。
    """
    conf = _text_check_cfg(cfg)
    base = {
        "ok": True,
        "enabled": conf["enabled"],
        "text_artifacts": [],
        "checked_shot_ids": [],
        "unchecked_shot_ids": [],
        "calls_used": 0,
        "model_used": None,
        "error": None,
    }
    if not conf["enabled"]:
        return base
    if not clips:
        return base

    own_tmp = None
    if tmp_dir is None:
        own_tmp = tempfile.mkdtemp(prefix="clip_text_check_")
        tmp_dir = own_tmp
    try:
        # 1) 各クリップから代表フレームを抽出し、フレーム item の平坦なリストを作る。
        #    expected_frames[shot] = そのショットで抽出できたフレーム数（=判定が揃うべき数）。
        frame_items: List[Dict[str, Any]] = []
        expected_frames: Dict[str, int] = {}
        ordered_shot_ids: List[str] = []
        for clip in clips:
            shot_id = clip.get("shot_id")
            clip_path = clip.get("clip_path")
            if not shot_id or not clip_path or not os.path.isfile(clip_path):
                continue
            times = _sample_times_for_clip(
                clip.get("duration_sec"), clip.get("start_sec", 0.0) or 0.0, conf["frames_per_clip"]
            )
            out_dir = os.path.join(tmp_dir, str(shot_id))
            frame_paths = extract_frames_at_times(ffmpeg_bin, clip_path, times, out_dir)
            if not frame_paths:
                continue
            if shot_id not in expected_frames:
                ordered_shot_ids.append(shot_id)
            expected_frames[shot_id] = expected_frames.get(shot_id, 0) + len(frame_paths)
            for t, fp in zip(times, frame_paths):
                frame_items.append({"shot_id": shot_id, "sample_sec": t, "frame_path": fp})

        if not frame_items:
            return base

        # 2) batch_size 単位で vision へ。max_vision_calls を超える分は「送らない」（安全キャップ）。
        batch_size = max(1, conf["vision_batch_size"])
        max_calls = max(0, conf["max_vision_calls"])
        by_shot: Dict[str, List[Dict[str, Any]]] = {}
        calls_used = 0
        model_used = None
        for start in range(0, len(frame_items), batch_size):
            if calls_used >= max_calls:
                break  # 上限超過分のフレームは未送信 → 該当ショットは checked にならない
            batch = frame_items[start:start + batch_size]
            res = inspect_frames_batch(batch, timeout_sec=conf["vision_timeout_sec"], _vision_fn=_vision_fn)
            calls_used += 1
            if not res.get("ok"):
                continue
            model_used = res.get("model_used") or model_used
            for v in res.get("verdicts", []):
                sid = v.get("shot_id")
                if sid is None:
                    continue
                by_shot.setdefault(sid, []).append(v)

        base["calls_used"] = calls_used
        base["model_used"] = model_used

        # 3) 抽出フレーム数ぶんの判定が全て揃ったショットだけを「検査済み」とする。
        checked: List[str] = []
        unchecked: List[str] = []
        artifacts: List[Dict[str, Any]] = []
        for shot_id in ordered_shot_ids:
            fvs = by_shot.get(shot_id, [])
            if fvs and len(fvs) >= expected_frames.get(shot_id, 0):
                checked.append(shot_id)
                art = _aggregate_shot_verdicts(shot_id, fvs)
                if art:
                    artifacts.append(art)
            else:
                unchecked.append(shot_id)
        base["checked_shot_ids"] = checked
        base["unchecked_shot_ids"] = unchecked
        base["text_artifacts"] = artifacts
        # 1ショットも検査できなかった場合は ok=False（検知不能）。一部未検査は ok=True のまま
        # unchecked_shot_ids で表す（=そのショットは警告も付けないが、クリーンとも主張しない）。
        if not checked:
            base["ok"] = False
            base["error"] = "vision 判定が取得できませんでした（全ショット未検査）"
        elif unchecked:
            base["error"] = "一部ショットが未検査です（呼び出し上限/応答欠落）: {}".format(",".join(unchecked))
        return base
    finally:
        if own_tmp is not None:
            import shutil as _shutil
            try:
                _shutil.rmtree(own_tmp, ignore_errors=True)
            except Exception:
                pass

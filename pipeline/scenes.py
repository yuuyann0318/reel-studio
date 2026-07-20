# -*- coding: utf-8 -*-
"""シーングループ（クリップ再利用）の純関数群。

「1文=1カット(1〜2秒)」の高速カットを、生成クレジットを増やさずに実現するための仕組み。
director が連続する2〜4ショットに同じ scene_id を付けると、それらは「同じ絵の別の瞬間」
として1本のシーンマスタークリップ（構成ショットの尺の合計）だけを生成し、以降は
その1本を各ショットの窓（trim_start〜）へ切り出して再利用する（=生成は1回・クレジットは1回分）。

この module はファイルI/Oやffmpeg実行を一切行わない純粋関数だけを置く（テスト容易性のため）。
実際の生成・切り出し・永続化は studio/server/jobs.py が本 module の計算結果を使って行う。

用語:
  - scene（シーン）: 同じ scene_id を持つ連続ショットの集合。{"scene_key", "shots":[...]}。
  - scene master: シーン1本ぶんに対して生成される合計尺のクリップ（clips/scene__<key>.mp4）。
  - window（窓）: シーンマスターの中で、各ショットが使う区間(trim_start〜trim_start+content)。
  - T_p（計画合計尺）: 構成ショットの duration_sec の合計。
  - T_a（実尺）: 生成・正規化後にffprobeで実測したシーンマスターの尺。

Python 3.9 互換構文のみ。
"""
from __future__ import annotations


def _shot_duration(shot):
    """director shot（duration_sec）／studio shot（source_duration）の両方から尺を読む。"""
    for key in ("duration_sec", "source_duration"):
        v = shot.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
    return 0.0


def _scene_id_of(shot):
    """非空文字列の scene_id を返す。無い/空/非文字列なら None（=単独シーン扱い）。"""
    sid = shot.get("scene_id")
    if isinstance(sid, str) and sid.strip():
        return sid.strip()
    return None


def group_shots_into_scenes(shots):
    """連続して同じ scene_id を持つショットを1つのシーンにまとめる。

    Returns: [{"scene_key": str, "shots": [shot, ...]}, ...]（入力順を保つ）。
      - scene_id を持つ連続ショット → scene_key = その scene_id。
      - scene_id 無しのショット → 単独シーン（scene_key = shot["id"]）。
      - 全ショットが scene_id 無しなら、各ショットが単独シーンになり従来経路と等価
        （呼び出し側で「1メンバーのシーンは従来どおり直接生成」する前提）。

    plan_schema.validate_plan が「同一 scene_id は連続ショットのみ」を保証している前提のため、
    ここでは単純に「直前ショットと同じ scene_id なら同じシーンへ足す」だけで正しくまとまる。
    """
    scenes = []
    current = None
    prev_sid = None
    for shot in shots:
        sid = _scene_id_of(shot)
        if sid is not None and sid == prev_sid and current is not None:
            current["shots"].append(shot)
        else:
            key = sid if sid is not None else shot.get("id")
            current = {"scene_key": key, "shots": [shot]}
            scenes.append(current)
        prev_sid = sid
    return scenes


def _suffix(i):
    """0->'a', 1->'b', ... 25->'z', 26->'aa' の bijective base-26 サフィックス。"""
    s = ""
    n = i + 1
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(ord("a") + r) + s
    return s


def split_scene_by_max_request(scene, max_sec=12.0):
    """合計尺が max_sec を超えるシーンを、ショット境界で貪欲に分割する。

    - 合計尺が max_sec 以下なら分割せず [scene]（scene_key はそのまま）を返す。
      「12秒ちょうど」は超過ではないので分割しない（境界は「max_sec を厳密に超えたら」）。
    - 超える場合はショット境界で貪欲に詰め、sub key に "_a" / "_b" ... を付ける（例: sc1_a, sc1_b）。
    - 単独ショット1本が max_sec を超える場合は、それ以上分割できないためそのショット1本だけの
      バケットになる（尺はそのまま。ハードエラーにはしない）。
    """
    shots = list(scene["shots"])
    key = scene["scene_key"]
    buckets = []
    current = []
    current_sum = 0.0
    for s in shots:
        d = _shot_duration(s)
        if current and (current_sum + d) > max_sec:
            buckets.append(current)
            current = []
            current_sum = 0.0
        current.append(s)
        current_sum += d
    if current:
        buckets.append(current)

    if len(buckets) <= 1:
        return [{"scene_key": key, "shots": shots}]
    return [
        {"scene_key": "{}_{}".format(key, _suffix(i)), "shots": b}
        for i, b in enumerate(buckets)
    ]


def compute_scene_windows(member_durations, actual_scene_duration):
    """シーンマスター（実尺 T_a）から各構成ショットが使う窓を計算する。

    各ショット j について {"trim_start", "content_duration", "pad_to"} を返す:
      - 計画オフセット off_j = j より前のショットの計画尺の合計。
      - 実尺 T_a が計画合計 T_p より短い場合のみ、開始オフセットを scale=T_a/T_p で比例圧縮する
        （trim_start = off_j * scale）。T_a >= T_p のときは圧縮しない（footage が足りている）。
      - content_duration = min(trim_start + dur_j, T_a) - trim_start
        （マスターから実際に取り出せる長さ）。
      - pad_to = dur_j（各ショットの出力尺は必ず計画尺 dur_j にそろえる。content が dur_j に満たない
        末尾ショットは、切り出し側の tpad=clone で最終フレームを静止延長して埋める）。
    ループ（マスターを巻き戻して先頭を再利用する）はしない＝足りない分は静止延長で埋めるだけ。

    Returns: [{"trim_start": float, "content_duration": float, "pad_to": float}, ...]
    """
    durations = [float(d) for d in member_durations]
    planned_total = sum(durations)
    try:
        t_a = float(actual_scene_duration)
    except (TypeError, ValueError):
        t_a = planned_total
    if t_a <= 0:
        t_a = planned_total

    scale = 1.0
    if planned_total > 0 and t_a < planned_total:
        scale = t_a / planned_total

    windows = []
    offset = 0.0
    for d in durations:
        start = offset * scale
        content = min(start + d, t_a) - start
        if content < 0.0:
            content = 0.0
        windows.append({"trim_start": start, "content_duration": content, "pad_to": d})
        offset += d
    return windows

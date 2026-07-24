# -*- coding: utf-8 -*-
"""build_shot_skeleton の単体テスト (TTP v2 Phase 2)。

reference_spec v2 から shots スケルトンを機械組み立てする純関数の検証。
実キャッシュspec (Minecraft frog pond, cuts=4/telops=2/sfx=70) を fixture として使う。
"""
import json
import os

import pytest

from pipeline import director


_FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "reference_spec_v2.json")


@pytest.fixture
def real_spec():
    with open(_FIXTURE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# (a) 比例スケール: 合計 = target ±0.1s
# ---------------------------------------------------------------------------

def test_build_shot_skeleton_scales_durations_proportionally_to_target(real_spec):
    for target in (15.0, 30.0, 45.0):
        sk = director.build_shot_skeleton(real_spec, target)
        total = sum(s["duration_sec"] for s in sk["shots"])
        assert abs(total - target) <= 0.1, "target={}: total={}".format(target, total)


def test_build_shot_skeleton_shot_count_matches_cuts_plus_one(real_spec):
    # cuts=4 → 5 shots
    sk = director.build_shot_skeleton(real_spec, 30.0)
    assert len(sk["shots"]) == 5


# ---------------------------------------------------------------------------
# (b) 最小尺保証
# ---------------------------------------------------------------------------

def test_build_shot_skeleton_enforces_min_shot_duration(real_spec):
    # 短い target を渡しても各shot >= 1.2秒を守ること
    sk = director.build_shot_skeleton(real_spec, 6.0)
    for s in sk["shots"]:
        assert s["duration_sec"] >= 1.2 - 0.001, s


# ---------------------------------------------------------------------------
# (c) telop → caption offset 写像
# ---------------------------------------------------------------------------

def test_build_shot_skeleton_maps_telops_to_caption_offsets(real_spec):
    sk = director.build_shot_skeleton(real_spec, 30.0)
    # 実spec の telops は s1 (start=2.486, end=4.972 -> 参考尺s1=[0,4.972])
    # と s2 (start=5.172, end=7.708 -> 参考尺s2=[4.972,7.708]) に載る
    assert "caption_in_offset_sec" in sk["shots"][0]
    assert "caption_out_offset_sec" in sk["shots"][0]
    assert "telop_style_hint" in sk["shots"][0]
    assert sk["shots"][0]["telop_style_hint"]["position"] == "top"
    assert sk["shots"][1]["telop_style_hint"]["position"] == "bottom"
    # 各 caption offset は対応 shot の duration_sec 内に収まる
    for shot in sk["shots"]:
        if "caption_in_offset_sec" in shot:
            assert 0.0 <= shot["caption_in_offset_sec"] <= shot["duration_sec"] + 0.01
            assert shot["caption_in_offset_sec"] <= shot["caption_out_offset_sec"] + 1e-6
            assert shot["caption_out_offset_sec"] <= shot["duration_sec"] + 0.01


# ---------------------------------------------------------------------------
# (d) sfx 間引き上限（70件 spec でも target/2.5 内に収まる）
# ---------------------------------------------------------------------------

def test_build_shot_skeleton_thins_sfx_events_within_global_and_per_shot_limits(real_spec):
    for target in (15.0, 30.0):
        sk = director.build_shot_skeleton(real_spec, target)
        # 全体上限 = floor(target / 2.5)
        assert len(sk["sfx_plan"]) <= int(target / 2.5) + 1
        # 各 shot 内は最大2発
        per_shot = {}
        for ev in sk["sfx_plan"]:
            sid = ev["t_anchor"]["shot_id"]
            per_shot[sid] = per_shot.get(sid, 0) + 1
        for sid, cnt in per_shot.items():
            assert cnt <= 2, "shot {} has {} sfx (limit=2)".format(sid, cnt)


# ---------------------------------------------------------------------------
# (e) kind → family マッピングと "other" 除外
# ---------------------------------------------------------------------------

def test_build_shot_skeleton_kind_to_family_mapping():
    # 各 shot に 1 発ずつ分散させて per-shot limit(=2)に引っかからないよう配置。
    spec = {
        "duration_sec": 25.0,
        "cuts": [{"t": 5.0}, {"t": 10.0}, {"t": 15.0}, {"t": 20.0}],
        "shots_ref": [],
        "telops": [],
        "sfx_events": [
            {"t": 1.0, "kind": "transition", "anchor": "cut", "confidence": 0.9},
            {"t": 6.0, "kind": "impact", "anchor": "none", "confidence": 0.9},
            {"t": 11.0, "kind": "riser", "anchor": "cut", "confidence": 0.9},
            {"t": 16.0, "kind": "pop", "anchor": "none", "confidence": 0.9},
            {"t": 21.0, "kind": "shimmer", "anchor": "none", "confidence": 0.9},
            {"t": 23.0, "kind": "other", "anchor": "none", "confidence": 0.99},  # 除外
        ],
    }
    sk = director.build_shot_skeleton(spec, 25.0)
    families = [ev["family"] for ev in sk["sfx_plan"]]
    assert "other" not in families
    # 各 known kind の family へ写像
    assert "whoosh" in families    # transition -> whoosh
    assert "impact" in families
    assert "riser" in families
    assert "pop" in families
    assert "shimmer" in families


def test_build_shot_skeleton_other_kind_is_dropped():
    spec = {
        "duration_sec": 10.0,
        "cuts": [],
        "shots_ref": [],
        "telops": [],
        "sfx_events": [{"t": 5.0, "kind": "other", "anchor": "none", "confidence": 1.0}],
    }
    sk = director.build_shot_skeleton(spec, 10.0)
    assert sk["sfx_plan"] == []


# ---------------------------------------------------------------------------
# (f) hook/cta shot_id 解決
# ---------------------------------------------------------------------------

def test_build_shot_skeleton_resolves_hook_end_and_cta_start_shot_ids(real_spec):
    # 実spec: hook_end_sec=7.708(s2の末端境界), cta_start_sec=19.753(s4の開始境界)
    # -> hook_end_shot_id は左寄せで s2、cta_start_shot_id は右寄せで s4。
    sk = director.build_shot_skeleton(real_spec, 30.0)
    assert sk["hook_end_shot_id"] == "s2"
    assert sk["cta_start_shot_id"] == "s4"


def test_build_shot_skeleton_hook_cta_none_when_spec_lacks_them():
    spec = {
        "duration_sec": 10.0,
        "cuts": [{"t": 5.0}],
        "shots_ref": [],
        "telops": [],
        "sfx_events": [],
    }
    sk = director.build_shot_skeleton(spec, 10.0)
    assert sk["hook_end_shot_id"] is None
    assert sk["cta_start_shot_id"] is None


# ---------------------------------------------------------------------------
# (g) 15字連続一致検出は director.run_director 側で拒否（骨検査+丸写し検査ペア）
# ---------------------------------------------------------------------------

def test_run_director_rejects_verbatim_overlap_in_caption_jp(monkeypatch):
    """caption_jp / narration_jp / narration_script のいずれかに参考テロップ文言の
    15字以上連続一致があると LLM 経路が矯正リトライへ回ることを確認する。"""
    spec = {
        "duration_sec": 10.0,
        "duration_sec": 10.0,
        "cuts": [{"t": 5.0}],
        "shots_ref": [],
        "transcript": "",
        "telops": [
            {"start": 0.0, "end": 5.0, "text": "これは参考テロップの丸写し禁止確認文言テストです",
             "position": "top", "style": {"color": "white", "stroke": "", "size_class": "large"}}
        ],
        "sfx_events": [],
    }

    calls = {"count": 0}

    # スケルトンを直接組み立てて期待値を得る
    skel = director.build_shot_skeleton(spec, 10.0)

    def _plan_with_caption(text):
        shots = []
        for s in skel["shots"]:
            shot = dict(s)
            shot["visual_prompt"] = "abstract test bg"
            shot["motion_preset"] = "static"
            # R3: caption_jp はスケルトンで caption_in_offset_sec が付いている shot だけに
            # 入れる（それ以外は空文字）。telop 数の骨検査を通過させるための整合。
            if "caption_in_offset_sec" in s:
                shot["caption_jp"] = text[:40]
            else:
                shot["caption_jp"] = ""
            shot["narration_jp"] = text
            shots.append(shot)
        return {
            "version": 2,
            "meta": {"source": "ai"},
            "concept": "テスト企画",
            "hook": "テストフック",
            "narration_script": text * len(shots),
            "shots": shots,
            "sfx_plan": skel["sfx_plan"],
            "hook_end_shot_id": skel["hook_end_shot_id"],
            "cta_start_shot_id": skel["cta_start_shot_id"],
            "bgm_mood": "upbeat",
        }

    def fake_call_claude_json(prompt, timeout_sec=600):
        calls["count"] += 1
        if calls["count"] == 1:
            # 1回目: 丸写し文言をそのまま返す(拒否されるはず)
            return {"ok": True, "data": _plan_with_caption("これは参考テロップの丸写し禁止確認文言テストです"),
                    "model_used": "m", "error": None}
        # 2回目以降: オリジナル文言に差し替え(合格)
        return {"ok": True, "data": _plan_with_caption("オリジナルな独自の文言に差し替えた完全別の言い方"),
                "model_used": "m", "error": None}

    monkeypatch.setattr(director, "call_claude_json", fake_call_claude_json)
    plan = director.run_director("テストテーマ", config={}, target_duration_sec=10.0,
                                   quality="single", reference=spec)
    assert calls["count"] >= 2, "丸写し検査が働いて矯正リトライが呼ばれるべき"
    # 最終 plan には丸写し文言が残っていないこと
    for s in plan["shots"]:
        assert "参考テロップの丸写し禁止確認文言" not in s["caption_jp"]


# ---------------------------------------------------------------------------
# skeleton の validate_plan 互換
# ---------------------------------------------------------------------------

def test_build_shot_skeleton_shots_pass_plan_v2_validate_with_ai_meta(real_spec):
    """スケルトンをそのまま plan にラップしても validate_plan (v2) が通ること。
    LLM 埋め字前に骨の妥当性を保証する。"""
    from pipeline import plan_schema
    sk = director.build_shot_skeleton(real_spec, 30.0)
    shots = []
    for s in sk["shots"]:
        shots.append({
            **s,
            "visual_prompt": "abstract placeholder",
            "motion_preset": "static",
            "caption_jp": "テスト",
            "narration_jp": "テスト",
        })
    plan = {
        "version": 2,
        "meta": {"source": "ai"},
        "concept": "テスト",
        "hook": "テスト",
        "narration_script": "テスト",
        "shots": shots,
        "sfx_plan": sk["sfx_plan"],
        "hook_end_shot_id": sk["hook_end_shot_id"],
        "cta_start_shot_id": sk["cta_start_shot_id"],
        "bgm_mood": "upbeat",
    }
    ok, errors, _ = plan_schema.validate_plan(plan, target_duration_sec=30.0, target_tolerance_sec=1.0)
    assert ok, errors


# ---------------------------------------------------------------------------
# fast-cut 参考動画のスケルトン破綻回避（cuts > target/1.2 の境界マージ）
# ---------------------------------------------------------------------------

def _fast_cut_spec(cuts_count, duration_sec):
    """cuts_count 個のカットを等間隔に置いた fast-cut spec を作る。"""
    step = duration_sec / (cuts_count + 1)
    cuts = [{"t": round(step * (i + 1), 3)} for i in range(cuts_count)]
    return {
        "duration_sec": float(duration_sec),
        "cuts": cuts,
        "shots_ref": [],
        "telops": [
            {"start": 0.5, "end": 2.5, "text": "hook", "position": "top",
             "style": {"color": "white", "stroke": "", "size_class": "large"}},
        ],
        "sfx_events": [
            {"t": round(step * (i + 1), 3), "kind": "transition", "anchor": "cut",
             "confidence": 0.9} for i in range(cuts_count)
        ],
    }


def test_build_shot_skeleton_fast_cut_merges_shots_to_respect_min_duration():
    """cuts=20 / target=15s（fast-cut）でも: 全 shot>=1.2s / 合計=target±0.1s。"""
    spec = _fast_cut_spec(cuts_count=20, duration_sec=8.0)
    sk = director.build_shot_skeleton(spec, 15.0)

    # (a) 全 shot >= 1.2s
    for s in sk["shots"]:
        assert s["duration_sec"] >= 1.2 - 0.001, s

    # (b) 合計 = target ±0.1s
    total = sum(s["duration_sec"] for s in sk["shots"])
    assert abs(total - 15.0) <= 0.1, total

    # (c) shot 数はマージにより floor(target/1.2)=12 以下
    assert len(sk["shots"]) <= 12

    # (d) sfx_plan の shot_id は全て実在する shot（マージ後）を指す
    valid_ids = {s["id"] for s in sk["shots"]}
    for ev in sk["sfx_plan"]:
        assert ev["t_anchor"]["shot_id"] in valid_ids, ev


def test_build_shot_skeleton_fast_cut_plan_v2_validates_after_merge():
    """fast-cut スケルトンをそのまま plan にラップしても validate_plan v2 が通る。"""
    from pipeline import plan_schema

    spec = _fast_cut_spec(cuts_count=20, duration_sec=8.0)
    sk = director.build_shot_skeleton(spec, 15.0)

    shots = []
    for s in sk["shots"]:
        shots.append({
            **s,
            "visual_prompt": "abstract placeholder",
            "motion_preset": "static",
            "caption_jp": "テスト",
            "narration_jp": "テスト",
        })
    plan = {
        "version": 2,
        "meta": {"source": "ai"},
        "concept": "テスト",
        "hook": "テスト",
        "narration_script": "テスト",
        "shots": shots,
        "sfx_plan": sk["sfx_plan"],
        "hook_end_shot_id": sk["hook_end_shot_id"],
        "cta_start_shot_id": sk["cta_start_shot_id"],
        "bgm_mood": "upbeat",
    }
    ok, errors, _ = plan_schema.validate_plan(plan, target_duration_sec=15.0, target_tolerance_sec=1.0)
    assert ok, errors

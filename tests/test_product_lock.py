# -*- coding: utf-8 -*-
"""商品1本ロック（診断#1・最大破綻対策）の単体テスト。

参考: TikTok @ami_biyou_ KINUI レビュー。生成は shot 毎に別ボトル4種が出現していた。
対策: 商品が映る全 shot に採用済み商品画像を --image-references として渡し、プロンプトへ
「参考画像とまったく同じ商品を出せ・別ボトルを発明するな」を機械追記する。商品が映らない
shot には「ボトルを出すな」を注記する。ここでは純関数 assign_images_to_shots の割当と、
mock backend のメタ記録を機械検証する。
"""
from pipeline import product_images
from pipeline.visual import mock_backend


def _shot(sid, desc_en):
    return {"id": sid, "visual_prompt": "a scene", "reference_visual": {"desc_en": desc_en}}


def _ref_spec(descs):
    return {"shots_ref": [{"start": i, "end": i + 1, "visual_desc_en": d} for i, d in enumerate(descs)]}


def test_all_product_shots_get_reference_image_and_lock_phrase():
    """商品shot全てに product 参照画像とロック句が付く。"""
    shots = [
        _shot("s1", "close-up of a serum bottle on a table"),
        _shot("s2", "a woman smiling at the camera"),
        _shot("s3", "the product bottle held in hand"),
    ]
    ref = _ref_spec([
        "close-up of a serum bottle on a table",
        "a woman smiling at the camera",
        "the product bottle held in hand",
    ])
    imgs = ["/abs/product_a.jpg", "/abs/product_b.jpg"]
    out = product_images.assign_images_to_shots(shots, imgs, reference_spec=ref)

    product_ref = imgs[0]
    # s1, s3 は商品shot。両方に product 参照画像（先頭採用画像）とロック句が付く。
    for idx in (0, 2):
        assert product_ref in (out[idx].get("reference_images") or []), out[idx]
        assert product_images._PRODUCT_LOCK_MARKER in out[idx]["visual_prompt"].lower()
    # s2 は非商品shot。ボトル抑止句が付く（ロック句は付かない）。
    assert product_images._PRODUCT_LOCK_MARKER not in out[1]["visual_prompt"].lower()
    assert product_images._NO_PRODUCT_MARKER in out[1]["visual_prompt"].lower()
    # 非商品shotには reference_images を付けない。
    assert not out[1].get("reference_images")


def test_product_ref_is_first_adopted_and_shared_across_shots():
    """複数商品shotでも同一の1枚（採用済み先頭）が全商品shotで共有される（＝商品1本ロック）。"""
    shots = [
        _shot("s1", "product bottle hero shot"),
        _shot("s2", "serum bottle close up"),
    ]
    ref = _ref_spec(["product bottle hero shot", "serum bottle close up"])
    imgs = ["/abs/canonical.jpg", "/abs/other.jpg"]
    out = product_images.assign_images_to_shots(shots, imgs, reference_spec=ref)
    assert out[0]["reference_images"][0] == "/abs/canonical.jpg"
    assert out[1]["reference_images"][0] == "/abs/canonical.jpg"


def test_no_product_indices_degrades_to_hook_cta_lock():
    """商品shotが検出できないときはフック+CTAをロック、中間は抑止句。"""
    shots = [
        _shot("s1", "a person walking"),
        _shot("s2", "a person talking"),
        _shot("s3", "a person waving"),
    ]
    ref = _ref_spec(["a person walking", "a person talking", "a person waving"])
    imgs = ["/abs/p.jpg"]
    out = product_images.assign_images_to_shots(shots, imgs, reference_spec=ref)
    assert product_images._PRODUCT_LOCK_MARKER in out[0]["visual_prompt"].lower()
    assert product_images._PRODUCT_LOCK_MARKER in out[2]["visual_prompt"].lower()
    assert product_images._NO_PRODUCT_MARKER in out[1]["visual_prompt"].lower()


def test_legacy_mode_unchanged_no_injection():
    """reference_spec=None（後方互換経路）ではプロンプト注入も reference_images も行わない。"""
    shots = [_shot("s1", "x"), _shot("s2", "y")]
    out = product_images.assign_images_to_shots(shots, ["/a.jpg", "/b.jpg"], reference_spec=None)
    for s in out:
        assert product_images._PRODUCT_LOCK_MARKER not in s["visual_prompt"].lower()
        assert product_images._NO_PRODUCT_MARKER not in s["visual_prompt"].lower()
        assert not s.get("reference_images")


def test_idempotent_no_double_injection():
    """既にロック句を含む shot を再割当しても二重追記しない。"""
    shots = [_shot("s1", "product bottle")]
    ref = _ref_spec(["product bottle"])
    once = product_images.assign_images_to_shots(shots, ["/a.jpg"], reference_spec=ref)
    twice = product_images.assign_images_to_shots(once, ["/a.jpg"], reference_spec=ref)
    assert twice[0]["visual_prompt"].lower().count(product_images._PRODUCT_LOCK_MARKER) == 1


def test_mock_backend_records_product_lock_meta(tmp_path, monkeypatch):
    """mock backend が商品ロックメタ（reference_images / product_locked）を記録する。"""
    # ffmpeg 実行はスキップして純粋にメタ記録だけ検証する。
    class _Proc:
        returncode = 0
        stderr = b""

    monkeypatch.setattr(mock_backend.subprocess, "run", lambda *a, **k: _Proc())
    be = mock_backend.MockBackend(cfg={})
    shot = {
        "id": "s1", "duration_sec": 4.0, "motion_preset": "static",
        "visual_prompt": "a hero shot. " + product_images._PRODUCT_LOCK_PHRASE,
        "reference_images": ["/abs/canonical.jpg"],
    }
    meta = be.generate(shot, str(tmp_path / "s1.mp4"))
    assert meta.get("reference_images") == ["/abs/canonical.jpg"]
    assert meta.get("product_locked") is True

    shot2 = {
        "id": "s2", "duration_sec": 4.0, "motion_preset": "static",
        "visual_prompt": "a person. " + product_images._NO_PRODUCT_PHRASE,
    }
    meta2 = be.generate(shot2, str(tmp_path / "s2.mp4"))
    assert meta2.get("no_product_directive") is True

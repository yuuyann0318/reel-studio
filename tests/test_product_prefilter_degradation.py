# -*- coding: utf-8 -*-
"""classify_product_images の堅牢化（診断B/C）テスト。

計測(ffmpeg)失敗 + vision失敗 が重なった画像は「誤画像を使うより無い方が安全」で
不採用へ縮退させる。片方でも成功していればその判断を尊重する。
実 ffmpeg/vision は呼ばず、color_stats_fn / dims_fn / vision_call をすべて注入する。
"""
from pipeline import product_images


def _vision_fail(prompt, batch, timeout_sec=600):
    return {"ok": False, "error": "all_models_failed"}


def _vision_ok_product(prompt, batch, timeout_sec=600):
    data = [
        {"index": i + 1, "category": "product_solo", "sharpness": "high", "dominant_colors": []}
        for i in range(len(batch))
    ]
    return {"ok": True, "data": data}


def test_measure_fail_and_vision_fail_degrades_to_not_adopted():
    paths = ["/tmp/a.png"]
    out = product_images.classify_product_images(
        paths,
        vision_call=_vision_fail,
        color_stats_fn=lambda p: None,   # 計測不能（ffmpeg失敗を模擬）
        dims_fn=lambda p: (800, 800),
    )
    assert len(out) == 1
    e = out[0]
    assert e["adopted"] is False
    assert e["reason"] == "unclassified_prefilter_and_vision_failed"


def test_measure_fail_but_vision_success_is_adopted():
    # 計測は失敗しても vision が product と判定できていれば採用する（vision を尊重）。
    paths = ["/tmp/a.png"]
    out = product_images.classify_product_images(
        paths,
        vision_call=_vision_ok_product,
        color_stats_fn=lambda p: None,
        dims_fn=lambda p: (800, 800),
    )
    assert out[0]["adopted"] is True
    assert out[0]["reason"] is None


def test_monochrome_excluded_by_prefilter_regardless_of_vision():
    # 単色/グラデ（ピンク装飾背景を模擬）は vision を呼ぶ前に除外される。
    paths = ["/tmp/pink.png"]
    vision_calls = {"n": 0}

    def _counting_vision(prompt, batch, timeout_sec=600):
        vision_calls["n"] += 1
        return _vision_ok_product(prompt, batch, timeout_sec)

    out = product_images.classify_product_images(
        paths,
        vision_call=_counting_vision,
        color_stats_fn=lambda p: {"pixels": 4096, "distinct": 3, "top1": 0.72, "top3": 0.9},
        dims_fn=lambda p: (1080, 1080),
    )
    assert out[0]["adopted"] is False
    assert out[0]["reason"] == "monochrome_or_gradient"
    assert vision_calls["n"] == 0  # 除外済みなので vision コストは発生しない


def test_measure_ok_and_vision_fail_still_adopts_all():
    # 計測は成功（実商品＝色種豊富）だが vision が落ちた場合は従来どおり縮退全採用
    # （計測で単色/グラデを否定できているのでピンク素通りの危険はない）。
    paths = ["/tmp/real.png"]
    out = product_images.classify_product_images(
        paths,
        vision_call=_vision_fail,
        color_stats_fn=lambda p: {"pixels": 4096, "distinct": 40, "top1": 0.30, "top3": 0.6},
        dims_fn=lambda p: (1080, 1080),
    )
    assert out[0]["adopted"] is True
    assert out[0]["reason"] == "vision_failed"

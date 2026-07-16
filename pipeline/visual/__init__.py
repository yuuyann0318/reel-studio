# -*- coding: utf-8 -*-
"""ビジュアル生成バックエンド群。config.json の "backend" で選択する。

get_backend(name) -> VisualBackend インスタンス。
"""
from __future__ import annotations

from pipeline.visual.base import VisualBackend  # noqa: F401


def get_backend(name, cfg=None):
    """バックエンド名からインスタンスを取得する。未知の名前はValueError。"""
    name = (name or "mock").lower()
    if name == "mock":
        from pipeline.visual.mock_backend import MockBackend
        return MockBackend(cfg)
    if name == "higgsfield":
        from pipeline.visual.higgsfield_backend import HiggsfieldBackend
        return HiggsfieldBackend(cfg)
    if name in ("cloudapi", "cloud_api"):
        from pipeline.visual.cloudapi_backend import CloudAPIBackend
        return CloudAPIBackend(cfg)
    raise ValueError("未知のbackend: {!r}（mock/higgsfield/cloudapi のいずれかを指定してください）".format(name))

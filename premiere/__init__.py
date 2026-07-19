# -*- coding: utf-8 -*-
"""Premiere Pro連携: Studioのplan -> FCP7 XML(xmeml) + SRT字幕 + 編集プロファイル生成。

- premiere.profile: assets/profiles/*.json のロード（manual_drive.json優先マージ）。
- premiere.srt: Studio plan -> SRT字幕テキスト。
- premiere.export_xmeml: Studio plan -> FCP7 XML(xmeml)シーケンス。
- premiere.package: 上記一式 + narration.wav + README をまとめた書き出しパッケージ生成（Phase A）。
- premiere.setup_check: pymiere自動インポートの実行可否チェック（Phase B）。
- premiere.driver: pymiere経由でPremiereへ自動インポートする本体（Phase B）。

Python 3.9 互換構文のみ。既存の pipeline/ 配下は編集せず、参照のみ行う
（pipeline.subtitles.wrap_caption_kinsoku を premiere.srt から利用）。
"""
from __future__ import annotations

from premiere.driver import run_import  # noqa: F401 (studio/server/jobs.py からの再エクスポート用)
from premiere.package import build_package  # noqa: F401 (studio/server/jobs.py からの再エクスポート用)
from premiere.setup_check import check_setup  # noqa: F401 (studio/server/jobs.py からの再エクスポート用)

__all__ = ["build_package", "check_setup", "run_import"]

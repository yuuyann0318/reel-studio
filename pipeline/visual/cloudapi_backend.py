# -*- coding: utf-8 -*-
"""Cloud API直叩きバックエンド（未実装スタブ）。

Higgsfield（または類似のtext-to-video/image-to-video SaaS）のREST APIを
Bearerトークンで直接叩く実装の雛形。CLIが提供されない/CLIより安定する場合の
代替経路として用意するが、本実装時点(2026-07-17)ではエンドポイント仕様
（URL・リクエストボディ・認証方式・ポーリングJSON形式）を未確認のため
実装していない。

想定する使い方（実装時のTODO）:
1. config.json の cloudapi.base_url / cloudapi.api_key_env を読む。
2. os.environ[api_key_env] からAPIキーを取得（無ければ明確なエラー）。
3. requests（または標準ライブラリの urllib）で
   POST {base_url}/videos {"prompt":..., "aspect_ratio":"9:16", "duration":...}
   → job_id を受け取る。
4. GET {base_url}/videos/{job_id} をポーリングし、status=="completed" になったら
   result_url をダウンロードして out_path に保存する。
5. 認証エラー(401/403)・レート制限(429)・タイムアウトを明確なエラーメッセージに変換する。

このスタブは呼び出されると常に NotImplementedError を送出する
（黙って失敗を握り潰さないため）。
"""
from __future__ import annotations

from pipeline.visual.base import VisualBackend


class CloudAPIBackend(VisualBackend):
    name = "cloudapi"

    def __init__(self, cfg=None):
        super().__init__(cfg)
        full_cfg = cfg or {}
        self.api_cfg = full_cfg.get("cloudapi", {}) or {}

    def submit_job(self, shot: dict) -> str:
        raise NotImplementedError(
            "cloudapi_backend は未実装のスタブです。base_url/認証方式が確定次第、"
            "pipeline/visual/cloudapi_backend.py の submit_job/poll_job/fetch_result を実装してください。"
        )

    def poll_job(self, job_id: str) -> dict:
        raise NotImplementedError("cloudapi_backend は未実装のスタブです。")

    def fetch_result(self, job_id: str, out_path: str) -> None:
        raise NotImplementedError("cloudapi_backend は未実装のスタブです。")

    def generate(self, shot: dict, out_path: str) -> dict:
        raise NotImplementedError(
            "cloudapi_backend は未実装のスタブです。config.jsonのbackendを 'mock' か 'higgsfield' にしてください。"
        )

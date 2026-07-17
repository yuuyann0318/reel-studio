# -*- coding: utf-8 -*-
"""VisualBackend 抽象IF。

すべての実装（mock/higgsfield/cloudapi）はこのインターフェースに従う。
`generate()` は同期的に呼び出し、内部で必要なら非同期ジョブのポーリングを
行って完成したクリップを out_path に書き出してから返る（呼び出し側=render.py
から見れば同期関数として扱える）。

非同期API（ジョブ投げてポーリングするタイプ）を実装する場合は、
`submit_job()` / `poll_job()` / `fetch_result()` の3つに分割して実装し、
基底クラスの `run_async_job()` を `generate()` から呼び出すとポーリングループが
共通化できる（higgsfield_backend.py 参照）。
"""
from __future__ import annotations

import time


class VisualBackendError(RuntimeError):
    """ビジュアル生成に失敗したことを示す例外。run.py はこれを捕捉してreportに記録する。"""


class VisualBackend:
    name = "base"

    def __init__(self, cfg=None):
        self.cfg = cfg or {}

    def generate(self, shot: dict, out_path: str) -> dict:
        """1ショット分の動画クリップを out_path に生成する。

        Args:
            shot: {"id","visual_prompt","motion_preset","duration_sec","caption_jp"}
                任意キー:
                - "image_path" (str): 商品画像のローカル絶対パス。指定時はこの画像を
                  起点にimage-to-video生成する（Higgsfieldバックエンドは `--start-image`
                  として渡す。mockバックエンドは実在すればffmpegの画像入力+Ken Burns風
                  ズームで擬似動画化する）。未指定/ファイル不在(mockのみ非エラーでフォール
                  バック。Higgsfieldは生成前にVisualBackendErrorで明確に失敗する)なら
                  従来どおりプロンプトのみのt2v/グラデーション生成にフォールバックする。
                - "reference_images" (list[str]): 見た目参照用の追加画像パス（最大9枚。
                  Higgsfieldバックエンドのみ使用。`--image-references` を1枚ごとに繰り返す）。
            out_path: 出力mp4パス（呼び出し側が用意したディレクトリ配下）

        Returns:
            メタ情報dict（少なくとも {"backend": self.name, "shot_id": shot["id"]} を含む）。

        Raises:
            VisualBackendError: 生成に失敗した場合。
        """
        raise NotImplementedError

    # -----------------------------------------------------------------
    # 非同期ジョブ型バックエンド向け共通ポーリングループ（任意で利用）
    # -----------------------------------------------------------------

    def submit_job(self, shot: dict) -> str:
        """ジョブを投げてjob_idを返す。非同期バックエンドはこれを実装する。"""
        raise NotImplementedError

    def poll_job(self, job_id: str) -> dict:
        """ジョブ状態を取得する。Returns: {"status": "pending"|"running"|"done"|"failed", "error": str|None}"""
        raise NotImplementedError

    def fetch_result(self, job_id: str, out_path: str) -> None:
        """完了済みジョブの成果物を out_path にダウンロードする。"""
        raise NotImplementedError

    def run_async_job(self, shot: dict, out_path: str, poll_interval_sec=5, poll_timeout_sec=600) -> dict:
        """submit_job → poll_job(ポーリング) → fetch_result の共通フロー。"""
        job_id = self.submit_job(shot)
        deadline = time.time() + poll_timeout_sec
        while True:
            status = self.poll_job(job_id)
            st = status.get("status")
            if st == "done":
                self.fetch_result(job_id, out_path)
                return {"backend": self.name, "shot_id": shot.get("id"), "job_id": job_id, "status": "done"}
            if st == "failed":
                raise VisualBackendError(
                    "{} ジョブ失敗 (job_id={}): {}".format(self.name, job_id, status.get("error") or "不明なエラー")
                )
            if time.time() > deadline:
                raise VisualBackendError(
                    "{} ジョブがタイムアウトしました (job_id={}, {}秒超過)".format(self.name, job_id, poll_timeout_sec)
                )
            time.sleep(poll_interval_sec)

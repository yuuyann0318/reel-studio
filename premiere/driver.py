# -*- coding: utf-8 -*-
"""Premiere Pro自動化(Phase B): pymiere経由で書き出しパッケージをPremiereへ自動インポートする。

run_import() は premiere.package.build_package() が生成したパッケージ
（reel.xml / captions.srt を含むディレクトリ）を受け取り、以下を順に試みる:
  1. package_dir/reel.prproj として新規プロジェクトを作成する
  2. reel.xml（FCP7 xmeml）をインポートする
  3. アクティブなシーケンスを取得する（activeSequence優先、無ければrootItem配下を
     videoTracksの有無で走査してシーケンスらしき項目を探す）
  4. captions.srt をインポートし、シーケンスにキャプショントラックとして追加する
     （`Sequence.createCaptionTrack` はExtendScript APIのバージョン差異で失敗しうるため、
     失敗時は縮退する: captions.srt自体はプロジェクトに読み込まれた状態のまま
     caption_track_created=False を返し、detailに手動手順を書く）
  5. プロジェクトを保存する

pymiereは関数内でimportする（未インストール環境でも premiere.driver 自体のimportは
落ちないようにするため）。すべての例外はここで捕捉し、日本語の理由付きで
{"ok": False, ...} を返す。呼び出し元（studio/server/jobs.py）はこの戻り値を見て
Phase A（パッケージ+README）へ静かにフォールバックする。

Python 3.9 互換構文のみ。
"""
from __future__ import annotations

import os


def _notify(progress_cb, pct, message):
    if progress_cb:
        progress_cb(pct, message)


def run_import(package_dir, project_id, progress_cb=None):
    """package_dir配下のPremiere書き出しパッケージをPremiere Proへ自動インポートする。

    Args:
        package_dir: premiere.package.build_package() が返した package_dir（str）。
        project_id: studio.server.projects のプロジェクトID（ログ/進捗メッセージ用途のみ）。
        progress_cb: callable(progress:int, message:str) | None。0〜100の目安（呼び出し側の
                     契約に合わせ90〜98あたりを想定）で進捗を通知する。

    Returns:
        dict: {
          "ok": bool,
          "prproj_path": str | None,   # 保存できた場合のみ.prprojの絶対パス
          "caption_track_created": bool,
          "detail": str,               # 日本語の説明（成功時も含む）
        }
    """
    try:
        import pymiere
    except Exception:
        return {
            "ok": False,
            "prproj_path": None,
            "caption_track_created": False,
            "detail": "pymiereがインストールされていないため、Premiereへの自動インポートをスキップしました。",
        }

    try:
        reel_xml_path = os.path.join(package_dir, "reel.xml")
        captions_srt_path = os.path.join(package_dir, "captions.srt")
        prproj_path = os.path.join(package_dir, "reel.prproj")

        if not os.path.exists(reel_xml_path):
            return {
                "ok": False,
                "prproj_path": None,
                "caption_track_created": False,
                "detail": "reel.xmlが見つからないため、Premiereへの自動インポートを中止しました: {}".format(reel_xml_path),
            }

        app = pymiere.objects.app

        _notify(progress_cb, 90, "Premiereで新規プロジェクトを作成中…")
        app.newProject(prproj_path)

        _notify(progress_cb, 92, "reel.xmlを読み込み中…")
        app.project.importFiles([reel_xml_path])

        _notify(progress_cb, 94, "シーケンスを確認中…")
        sequence = _find_sequence(app)
        if sequence is None:
            try:
                app.project.save()
            except Exception:
                pass
            return {
                "ok": False,
                "prproj_path": str(prproj_path),
                "caption_track_created": False,
                "detail": "reel.xmlは読み込みましたが、シーケンスが見つかりませんでした。Premiere上で手動確認してください。",
            }

        caption_track_created = False
        detail = "Premiereに組み上げました。"
        if os.path.exists(captions_srt_path):
            _notify(progress_cb, 96, "字幕(captions.srt)を読み込み中…")
            try:
                srt_item = _import_srt(app, captions_srt_path)
                if srt_item is None:
                    raise RuntimeError("captions.srtのプロジェクト項目が見つかりませんでした")
                sequence.createCaptionTrack(srt_item, 0, "SRT")
                caption_track_created = True
            except Exception:
                caption_track_created = False
                detail = (
                    "字幕トラックの自動作成に失敗しました。captions.srtはプロジェクトに読み込み済みなので、"
                    "タイムラインへドラッグしてください。"
                )

        _notify(progress_cb, 98, "プロジェクトを保存中…")
        app.project.save()

        return {
            "ok": True,
            "prproj_path": str(prproj_path),
            "caption_track_created": caption_track_created,
            "detail": detail,
        }
    except Exception as exc:
        return {
            "ok": False,
            "prproj_path": None,
            "caption_track_created": False,
            "detail": "Premiereへの自動インポート中にエラーが発生しました: {}".format(exc),
        }


def _find_sequence(app):
    """app.project.activeSequence優先、無ければrootItem配下をvideoTracksの有無で走査する。

    Premiere ExtendScriptのSequenceオブジェクトは videoTracks を持つが、通常の
    ProjectItem（クリップ/ビン）は持たないため、これをシーケンス判定の目印にする。
    """
    sequence = getattr(app.project, "activeSequence", None)
    if sequence is not None:
        return sequence
    try:
        root_item = app.project.rootItem
        count = root_item.children.numItems
        for i in range(count):
            child = root_item.children[i]
            if hasattr(child, "videoTracks"):
                return child
    except Exception:
        pass
    return None


def _import_srt(app, captions_srt_path):
    """captions.srtをプロジェクトへインポートし、対応するProjectItemを返す。"""
    app.project.importFiles([captions_srt_path])
    return _find_project_item_by_name(app, os.path.basename(captions_srt_path))


def _find_project_item_by_name(app, name):
    root_item = app.project.rootItem
    count = root_item.children.numItems
    for i in range(count):
        child = root_item.children[i]
        if getattr(child, "name", None) == name:
            return child
    return None

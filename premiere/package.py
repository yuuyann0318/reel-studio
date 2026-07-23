# -*- coding: utf-8 -*-
"""Studio plan -> Premiere Pro書き出しパッケージ（Phase A配線）。

build_package(project_id, progress_cb=None) が
projects/<id>/premiere/<YYYYmmddHHMMSS>_<uuid8>/ に以下一式を生成する
（uuid8サフィックスは同一プロジェクトへの短時間の二重投入でもpackage_dirが
衝突しないようにするため。studio/server/jobs.pyのtry_start_premiere_exportが
同一project_idの同時実行を409で拒否するのと合わせた二重の防御）:
  - narration.wav   : plan.narration_text をTTSで再合成した音声
  - reel.xml        : premiere.export_xmeml.build_xmeml() のFCP7 XML(xmeml)シーケンス
  - captions.srt    : premiere.srt.build_srt() のSRT字幕
  - style/STYLE_SPEC.md : assets/premiere/STYLE_SPEC.md のコピー（テロップ見た目の仕様書。
                           Premiereの.prtextstyleは自動生成できないためのプレースホルダ）
  - README_import.md    : 日本語・初心者向けの取り込み手順

premiere.profile / premiere.srt / premiere.export_xmeml、studio.server.projects、
pipeline.tts を「利用」するのみで、これらのモジュール自体は編集しない。

Python 3.9 互換構文のみ。
"""
from __future__ import annotations

import json
import shutil
import time
import uuid

from pipeline import tts as tts_mod
from pipeline.config import load_config, project_root
from premiere import export_xmeml
from premiere import profile as profile_mod
from premiere import srt as srt_mod
from studio.server import projects

DEFAULT_PROFILE_NAME = "ttp_reference"

STYLE_SPEC_SRC = project_root() / "assets" / "premiere" / "STYLE_SPEC.md"

# assets/premiere/STYLE_SPEC.md が万一見つからない場合でも空パッケージにしないための
# 最小フォールバック本文（通常運用では到達しない想定。実ファイルは同梱済み）。
_FALLBACK_STYLE_SPEC_TEXT = (
    "# テロップ スタイル仕様（STYLE_SPEC）\n\n"
    "白文字＋黒の細い縁取り＋Noto Sans JP Black・中央やや上に配置してください。\n"
    "詳細な仕様ファイル(assets/premiere/STYLE_SPEC.md)が見つからなかったため、この簡易版を出力しています。\n"
)


class PremierePackageError(RuntimeError):
    """書き出しパッケージの生成に失敗した場合に送出する。"""


def _now_dirname():
    """<YYYYmmddHHMMSS>_<uuid8> 形式の出力ディレクトリ名を作る。

    タイムスタンプ(秒精度)だけだと、同一プロジェクトへ短時間に複数回
    build_package()が呼ばれた場合（例: 「Premiereで編集」の連打・二重投入）に
    同じ package_dir へ衝突し、片方の書き出し内容が破損/上書きされるおそれがある。
    uuid4().hex[:8] のサフィックスを付けて呼び出しごとに一意にする。
    """
    return "{}_{}".format(time.strftime("%Y%m%d%H%M%S"), uuid.uuid4().hex[:8])


def _missing_media_section(warnings):
    """build_xmeml の warnings (missing_media) を README 用のセクション文字列にする。

    素材ファイルが実在しない場合、Premiere取り込み時に「メディアがオフラインです」と
    表示される。事前にファイル一覧を明記して、再リンクや素材の再収集を促す。
    """
    if not warnings:
        return ""
    missing = [w for w in warnings if w.get("kind") == "missing_media"]
    if not missing:
        return ""
    lines = ["", "## ⚠️ オフラインになるファイル一覧", ""]
    lines.append("このパッケージには、書き出し時点で見つからなかった素材への参照が含まれます。")
    lines.append("Premiere取り込み後、以下のクリップは「メディアがオフラインです」と表示されます。")
    lines.append("実ファイルを配置し直すか、Premiereの「メディアの再リンク」で場所を指定してください。")
    lines.append("")
    lines.append("| トラック | ファイル名 | 参照パス |")
    lines.append("| --- | --- | --- |")
    for w in missing:
        lines.append("| {} | {} | `{}` |".format(w.get("role", ""), w.get("name", ""), w.get("abs_path", "")))
    lines.append("")
    return "\n".join(lines)


def _track_contract_section():
    """トラック構成規約の README セクション（premiere.export_xmeml の規約と一致）。"""
    return (
        "\n## トラック構成（規約）\n\n"
        "このパッケージのシーケンス `reel_sequence` は以下の固定順で書き出されます。\n\n"
        "| トラック | 用途 | 色ラベル | 命名 |\n"
        "| --- | --- | --- | --- |\n"
        "| V1 | 本編クリップ | Iris | `S01` `S02` … |\n"
        "| V2 | テロップ用の予約（空） | — | — |\n"
        "| V3 | 演出（赤丸オーバーレイ等） | Rose | `RC01 red_circle` |\n"
        "| A1 | ナレーション | Forest | `NAR narration` |\n"
        "| A2 | BGM | Lavender | `BGM bgm` |\n"
        "| A3 | 効果音（SE） | Caribbean | `SE01 whoosh` 等（先頭語=family） |\n\n"
        "シーケンス直下にはショット境界・フック終了/CTA開始・SFXの意図を"
        "`marker` として書き出しています（Premiereのシーケンスマーカーとして表示）。\n"
    )


def _build_readme_text(project_id, missing_media_section=""):
    return (
        "# Premiereでの読み込み方\n\n"
        "このフォルダの中身を読み込むと、字幕付きの編集済みプロジェクトとしてPremiere Proで開けます。\n\n"
        "## 手順\n\n"
        "1. Premiereのメニューから「ファイル > 読み込み」を選び、このフォルダの `reel.xml` を選んでください。\n"
        "2. プロジェクトパネルに現れたシーケンス（`reel_sequence`）をダブルクリックして開いてください。\n"
        "3. `captions.srt` をプロジェクトパネルへ読み込み、タイムラインへドラッグしてください"
        "（自動でキャプション（字幕）トラックになります）。\n"
        "4. 「ウィンドウ > エッセンシャルグラフィックス」を開き、「キャプション」タブの「トラックスタイル」から、"
        "テロップの見た目（下記「初回だけの準備」で作成したスタイル）を1クリックで適用してください。\n"
        "5. 字幕の文字や位置は、Premiere上でいつでも自由に編集できます。\n\n"
        "## 初回だけの準備（テロップの見た目を保存する）\n\n"
        "Premiereの「トラックスタイル」ファイル（.prtextstyle）は、Adobeの内部形式のためこのツールから"
        "自動生成できません。代わりに `style/STYLE_SPEC.md` に見た目の仕様"
        "（白文字＋黒の細い縁取り＋Noto Sans JP Black・中央やや上・サイズの目安）をまとめています。\n"
        "初回だけ、Premiere上でこの仕様どおりにテキストスタイルを作成し、「トラックスタイルとして保存」して"
        "ください。一度保存すれば、次回以降の動画では手順4でワンクリックに使い回せます。\n\n"
        "## うまくいかないとき\n\n"
        "- 「メディアがオフラインです」と表示される場合: 素材（映像クリップ）の保存場所は "
        "`projects/{project_id}/clips/` です。Premiereの「メディアの再リンク」からこのフォルダを"
        "指定してください。\n"
        "- 音声が聞こえない場合: `narration.wav` がA1トラックに読み込まれているか確認してください。\n"
        "- 字幕が出ない場合: `captions.srt` をタイムラインへドラッグ済みか、字幕トラックが表示（有効）に"
        "なっているか確認してください。\n"
        "- `timeline.json`（機械可読）: このパッケージに同梱されているサイドカーファイルです。"
        "各ショットの表示区間・テロップ表示秒・SE配置秒・BGM音量カーブが記載されており、"
        "自作ツールで同期意図を再利用したいとき参照できます。\n"
        + _track_contract_section()
        + (missing_media_section or "")
    ).format(project_id=project_id)


def build_package(project_id, progress_cb=None):
    """project_idのplan（編集結果）からPremiere書き出しパッケージ一式を生成する。

    Args:
        project_id: studio.server.projects のプロジェクトID。
        progress_cb: callable(progress:int, message:str) | None。0〜100の目安で進捗を通知する
                     （呼び出し側=studio/server/jobs.pyがSSEイベントへ変換する）。

    Returns:
        dict: {"package_dir": str, "files": list[str], "tts": dict, "profile_name": str}

    Raises:
        PremierePackageError: プロジェクトが存在しない場合。
    """
    def _progress(pct, message):
        if progress_cb:
            progress_cb(pct, message)

    project = projects.get_project(project_id)
    if project is None:
        raise PremierePackageError("プロジェクトが見つかりません: {}".format(project_id))
    plan = project.get("plan") or {}
    cfg = load_config()

    pdir = projects.project_dir(project_id)
    package_dir = pdir / "premiere" / _now_dirname()
    package_dir.mkdir(parents=True, exist_ok=True)

    # (a) narration.wav ---------------------------------------------------
    _progress(10, "ナレーション音声を生成中…")
    narration_path = package_dir / "narration.wav"
    tts_backend = tts_mod.get_tts_backend(voice=cfg.get("voice", "Kyoko"), cfg=cfg)
    tts_meta = tts_backend.synthesize(plan.get("narration_text", "") or "", str(narration_path), cfg)

    # (b) reel.xml ----------------------------------------------------------
    _progress(45, "編集プロファイルを読み込み中…")
    prof = profile_mod.load_profile(name=DEFAULT_PROFILE_NAME)

    _progress(55, "Premiere用シーケンス(reel.xml)を作成中…")
    bgm_cfg = plan.get("bgm") or {}
    bgm_rel = bgm_cfg.get("file") if isinstance(bgm_cfg, dict) else None
    bgm_path = None
    if bgm_rel:
        resolved = projects.resolve_bgm_path(bgm_rel)
        bgm_path = str(resolved) if resolved else None

    # Phase B: plan v2 の sfx_plan / caption offset / hook-cta を Premiere タイムラインへ
    # 忠実に反映するため、shot_display_durations と sfx_events / bgm_curve を先に解決する。
    # ここでは xmeml と ffmpeg レンダで「同じ入力から同じ時刻」が出るように、既存の
    # pipeline.render.compute_edit_enhancement_kwargs() をそのまま流用する
    # （＝ffmpeg レンダ経路と時刻源が同じ→ ±1フレームでの同期一致を保証する）。
    shot_display_durations = _resolve_shot_display_durations(plan)
    sfx_events = _resolve_plan_sfx_events(plan)
    enhancement_bgm_curve = None
    try:
        from pipeline import edit_profile as _ep_mod
        from pipeline import render as _render_mod
        _edit_prof = _ep_mod.load_edit_profile(cfg, project_seed=project_id)
        _enh = _render_mod.compute_edit_enhancement_kwargs(
            shot_display_durations, _edit_prof, project_seed=project_id, plan=plan,
        )
        sfx_events = sfx_events + (_enh.get("sfx_extra") or [])
        enhancement_bgm_curve = _enh.get("bgm_curve")
    except Exception:
        # 編集プロファイルの読み込み失敗 → v1 経路（plan["sfx"] のみ・BGMカーブなし）へ縮退
        _edit_prof = None

    # shots[].clip_path は "projects/<id>/clips/<file>" 形式（studio.server.projects.
    # media_relpath_for_clip契約）で、PROJECTS_ROOT.parent基点の相対パスとして解決される
    # （projects.resolve_media_relpathと同じ基点。pipeline.config.project_root()と本番では
    # 一致するが、PROJECTS_ROOTを差し替えるテスト等でも正しく解決できるようこちらを使う）。
    export_base_dir = projects.PROJECTS_ROOT.parent
    xmeml_result = export_xmeml.build_xmeml(
        plan, str(export_base_dir),
        narration_path=str(narration_path), bgm_path=bgm_path, profile=prof,
        shot_display_durations=shot_display_durations,
        sfx_events=sfx_events,
        bgm_curve=enhancement_bgm_curve,
        emit_captions=True,
        return_timeline=True,
    )
    xmeml_text = xmeml_result["xmeml"]
    xmeml_warnings = xmeml_result.get("warnings") or []
    timeline_data = xmeml_result.get("timeline") or {}
    reel_xml_path = package_dir / "reel.xml"
    reel_xml_path.write_text(xmeml_text, encoding="utf-8")

    # timeline.json サイドカー（機械可読な同期意図の要約。README にも一言案内する）
    timeline_json_path = package_dir / "timeline.json"
    timeline_json_path.write_text(
        json.dumps(timeline_data, ensure_ascii=False, indent=2), encoding="utf-8",
    )

    # (c) captions.srt --------------------------------------------------------
    _progress(70, "字幕(captions.srt)を作成中…")
    srt_text = srt_mod.build_srt(plan)
    captions_path = package_dir / "captions.srt"
    captions_path.write_text(srt_text, encoding="utf-8")

    # (d) style/ -------------------------------------------------------------
    _progress(80, "テロップスタイルの仕様書を準備中…")
    style_dir = package_dir / "style"
    style_dir.mkdir(parents=True, exist_ok=True)
    style_spec_dest = style_dir / "STYLE_SPEC.md"
    if STYLE_SPEC_SRC.exists():
        shutil.copyfile(str(STYLE_SPEC_SRC), str(style_spec_dest))
    else:
        style_spec_dest.write_text(_FALLBACK_STYLE_SPEC_TEXT, encoding="utf-8")

    # (e) README_import.md -----------------------------------------------------
    _progress(90, "使い方(README)を作成中…")
    readme_path = package_dir / "README_import.md"
    missing_section = _missing_media_section(xmeml_warnings)
    readme_path.write_text(
        _build_readme_text(project_id, missing_media_section=missing_section),
        encoding="utf-8",
    )

    # (f) 返り値 ---------------------------------------------------------------
    files = [
        str(reel_xml_path),
        str(captions_path),
        str(timeline_json_path),
        str(narration_path),
        str(style_spec_dest),
        str(readme_path),
    ]
    return {
        "package_dir": str(package_dir),
        "files": files,
        "tts": tts_meta,
        "profile_name": DEFAULT_PROFILE_NAME,
        "xmeml_warnings": xmeml_warnings,
        "timeline": timeline_data,
    }


# ---------------------------------------------------------------------------
# 内部ヘルパ（Phase B）
# ---------------------------------------------------------------------------

def _resolve_shot_display_durations(plan):
    """enabled ショットの表示尺(秒)を、V1 タイムラインと同じ規約で並べて返す。

    xmeml._build_v1_clips が使う「order でソート → trim.end - trim.start」を再現する。
    trim が無いショットは duration_sec を採用する（plan v2 の名目尺フォールバック）。
    plan.shots と同順（enabled_shots 順）で返すため、pipeline.sfx_planner が期待する
    「plan.shots と同順の durations」ではなく enabled_shots 相当。呼び出し側では
    enabled_shots のみで sfx_plan.t_anchor.shot_id を解決する運用に揃える。
    """
    shots = (plan or {}).get("shots") or []
    enabled = sorted(
        [s for s in shots if isinstance(s, dict) and s.get("enabled", True)],
        key=lambda s: s.get("order", 0),
    )
    durations = []
    for s in enabled:
        trim = s.get("trim") or {}
        if "start" in trim and "end" in trim:
            dur = max(0.0, float(trim["end"]) - float(trim["start"]))
        else:
            dur = float(s.get("duration_sec", 0.0) or 0.0)
        durations.append(dur)
    return durations


def _resolve_plan_sfx_events(plan):
    """plan["sfx"] の各エントリ({file, at_sec, gain_db}) を、export_xmeml が受ける
    sfx_events 形式({path, at_sec, gain_db}) に変換する。file が解決不能なものはスキップ。
    """
    events = []
    for s in (plan or {}).get("sfx") or []:
        if not isinstance(s, dict):
            continue
        f = s.get("file")
        if not f:
            continue
        resolved = projects.resolve_sfx_path(f)
        if resolved is None:
            continue
        events.append({
            "path": str(resolved),
            "at_sec": float(s.get("at_sec", 0.0) or 0.0),
            "gain_db": s.get("gain_db"),
        })
    return events

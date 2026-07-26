# -*- coding: utf-8 -*-
"""Higgsfield CLI バックエンド（実CLI仕様確認済み・2026-07-17）。

実機で確認したCLI仕様（`higgsfield --help` 系を実行して確認済み。バージョン
`higgsfield 1.1.17`）:

  - 投入: `higgsfield generate create <job_type> --prompt "..." --aspect-ratio 9:16
    --resolution 480p|720p --duration <int秒> --json`
    → stdoutはジョブIDの JSON配列（例 `["2296cac2-...-...-..."]`）。
  - 完了待ち: `higgsfield generate wait <job_id> --timeout <dur> --interval <dur> --json`
    → JSONオブジェクト。`status` が `"completed"` で `result_url` にmp4のURL。
    `status` が `"failed"`/`"error"` ならエラー。CLI自身の `--timeout` 超過時は
    非ゼロ終了 + stderrにタイムアウト文言（保険としてPython側subprocessにも
    余裕を持たせたタイムアウトを設定する）。
  - コスト見積: `higgsfield generate cost <job_type> --prompt "..." --aspect-ratio 9:16
    --resolution 480p --duration 5 --json` → `{"credits": <int>}`。
  - 未認証時: stderrに "Not authenticated"（大小文字は揺れうる）を含む。
  - モデル(seedance_2_0_mini)側のduration制約（BUG-10・実機確認）: `--duration` に
    3秒等の短い値を渡すと `duration: Input should be greater than or equal to 4` という
    バリデーションエラー(exit code 3)を返しジョブが投入されない。最小4秒が必要
    （`_resolve_request_duration_sec()` でクランプして対処）。

コマンド構築・レスポンス解釈は `_build_*_cmd()` / `_parse_*()` 関数に隔離してあり、
CLI側の仕様変更が起きてもこれらの関数だけ直せば追従できる設計にしてある。

CLI未インストール時は `FileNotFoundError` を捕捉して明確なエラーメッセージに変換する。

Python 3.9 互換構文のみ。
"""
from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import time
from typing import List, Optional  # noqa: F401 — _augment_prompt_with_reference_visual で使用

from pipeline.config import load_config, project_root
from pipeline.visual.base import VisualBackend, VisualBackendError

# `higgsfield` バイナリが PATH に無い環境向けの明示フォールバック（node同梱パス）。
# 絶対ユーザー名を含めないため HOME 基準で解決する（この Mac では従来と同じ場所を指す）。
_NODE_BIN_FALLBACK = os.path.join(
    os.path.expanduser("~"), "claude code", "node-v22.11.0-darwin-arm64", "bin", "higgsfield"
)

_DEFAULT_MODEL = "seedance_2_0_mini"
_DEFAULT_RESOLUTION = "480p"
_DEFAULT_MAX_CREDITS_PER_SHOT = 10

# ★BUG-10（実機確認・2026-07-17, higgsfield CLI 1.1.17, model=seedance_2_0_mini）:
# directorが3秒ショットを計画し、そのduration_secをそのまま `--duration` に渡したところ、
# CLIが `duration: Input should be greater than or equal to 4` というバリデーションエラー
# (exit code 3) を返し、ジョブが1件も投入されずフルリール生成が失敗した。
# モデル側の最小duration制約が4秒であることを実機エラーで確認したため定数化する。
_MIN_REQUEST_DURATION_SEC = 4
# 上限側は実機のエラーで確認したものではない（未検証）。暴走的なコスト増加・過大な
# クレジット消費を防ぐための保守的なキャップとして設定する。実際の計画尺より長く
# リクエストした分は、後段の render.build_normalize_clip_cmd（duration_sec指定）で
# 計画どおりの尺にトリムして吸収する。
_MAX_REQUEST_DURATION_SEC = 12


_MAX_REFERENCE_IMAGES = 9


def _build_image_args(shot):
    """shotの image_path / reference_images から画像関連の追加CLIフラグを構築する（副作用なし・テスト対象）。

    - image_path（商品画像のローカル絶対パス、非空）: `--start-image <path>` を1つ追加
      （image-to-video。Higgsfield CLIはローカルパスを自動アップロードする＝実機確認済み）。
    - reference_images（list、任意）: 最大 `_MAX_REFERENCE_IMAGES`(9) 枚にクリップし、
      1枚ごとに `--image-references <path>` を繰り返し追加する（CLIヘルプ実測:
      "use repeated --image-references"）。
    - どちらも無い/空のshotでは何も追加しない＝画像なしshotのコマンドは従来と完全に同一（回帰）。
    """
    args = []
    image_path = shot.get("image_path")
    if image_path:
        args += ["--start-image", str(image_path)]
    reference_images = shot.get("reference_images") or []
    for ref_path in list(reference_images)[:_MAX_REFERENCE_IMAGES]:
        args += ["--image-references", str(ref_path)]
    return args


def _resolve_request_duration_sec(shot):
    """ショット尺(shot['duration_sec'])から、CLIへ実際にリクエストする秒数を決める。

    副作用なし・テスト対象。ショット尺をそのままモデルへ渡すと、モデルの最小/最大
    duration制約に反してCLIがバリデーションエラーで失敗することがある(BUG-10)。
    ceil(ショット尺) を [_MIN_REQUEST_DURATION_SEC, _MAX_REQUEST_DURATION_SEC] にクランプ
    して安全な値でリクエストし、実際の最終カット尺は plan.shots[].duration_sec を正として
    後段のトリム処理に委ねる。
    """
    duration_sec = float(shot.get("duration_sec", 5))
    requested = int(math.ceil(duration_sec))
    return max(_MIN_REQUEST_DURATION_SEC, min(_MAX_REQUEST_DURATION_SEC, requested))


class HiggsfieldAuthError(VisualBackendError):
    """`higgsfield auth login` が必要（認証切れ/未認証）な場合。"""


class HiggsfieldTimeoutError(VisualBackendError):
    """ジョブ待機がタイムアウトした場合。"""


class HiggsfieldJobFailedError(VisualBackendError):
    """ジョブがCLI/サーバ側で失敗ステータスになった場合。"""


class HiggsfieldCostLimitError(VisualBackendError):
    """見積コストが max_credits_per_shot を超えるため、ジョブを投入せず中断した場合。"""


class HiggsfieldCreditError(VisualBackendError):
    """クレジット残高不足（`not_enough_credits`）で失敗した場合。

    ユーザー側の対処(チャージ)が必要で自動復旧しないため、専用の例外クラスとして
    _call_with_retry で例外型により明示的にリトライ対象から除外する(メッセージ文字列
    への_is_transient_error適用に依存しない。日本語ラップ後のメッセージにstderrの
    原文が埋め込まれると、stderrに"connection"等のtransientパターン文字列がたまたま
    含まれていた場合に誤ってリトライ対象と判定されてしまう脆さがあったため)。
    """


class HiggsfieldSafetyRejectedError(VisualBackendError):
    """安全フィルタ(nsfw/moderation/flagged)によりジョブが拒否された場合（1回の試行内部で使う中間例外）。

    generate() 内の安全リトライループがこれを捕捉してプロンプトを差し替え再試行する。
    最終試行でも拒否された場合は generate() が日本語の VisualBackendError に変換して送出する。
    """


def _resolve_cli_bin(configured):
    """設定値からCLIの実行パスを解決する。

    絶対パスで実在すればそのまま使う。PATH解決可能ならそれを使う。
    どちらも失敗したら node 同梱パスへ明示フォールバックする（それも無ければ
    設定値をそのまま返し、実行時の FileNotFoundError で明確なエラーにする）。
    """
    configured = configured or "higgsfield"
    if os.path.isabs(configured) and os.path.exists(configured):
        return configured
    found = shutil.which(configured)
    if found:
        return found
    if os.path.exists(_NODE_BIN_FALLBACK):
        return _NODE_BIN_FALLBACK
    return configured


_FRAMING_PHRASES = {
    "rule_of_thirds": "rule-of-thirds framing",
    "center": "centered framing",
    "symmetric": "symmetric framing",
    "low_angle": "low-angle framing",
    "high_angle": "high-angle framing",
    "eye_level": "eye-level framing",
    "over_the_shoulder": "over-the-shoulder framing",
    "aerial": "aerial framing",
    "dutch_angle": "dutch-angle framing",
    "top_down": "top-down framing",
    "closeup_face": "close-up face framing",
    "product_on_hand": "product-on-hand framing",
    "flatlay": "flatlay top-down framing",
}


def _camera_phrase(cam: str, intensity: str) -> "Optional[str]":
    """camera_move ラベルと intensity を英語のカメラ句に変換する。未対応ラベルは None。"""
    cam = (cam or "").strip().lower()
    if cam in ("pan_l", "pan_left"):
        phrase = "camera pans to the left"
    elif cam in ("pan_r", "pan_right"):
        phrase = "camera pans to the right"
    elif cam == "tilt_up":
        phrase = "camera tilts up"
    elif cam == "tilt_down":
        phrase = "camera tilts down"
    elif cam == "zoom_in":
        phrase = "camera slowly zooms in"
    elif cam == "zoom_out":
        phrase = "camera slowly zooms out"
    elif cam == "handheld":
        phrase = "handheld camera with natural micro-motion"
    elif cam == "dolly":
        phrase = "smooth dolly move"
    else:
        return None
    intensity = (intensity or "").strip().lower()
    if intensity == "strong":
        phrase = phrase.replace("slowly ", "").replace("with natural ", "with pronounced ")
        phrase = phrase + " (strong)"
    elif intensity == "weak":
        if "slowly" not in phrase and "natural" not in phrase:
            phrase = "subtle " + phrase
    return phrase


def _augment_prompt_with_reference_visual(base_prompt, shot):
    """P2: 「参考shotの再現」を主文に据え、商品/テーマは挿入句として従属させる。

    従来（R2b F4）は base_prompt = 商品/テーマ主導の LLM 出力に対して、カメラ・構図・色調を
    末尾補足として足すだけだったため、visual_prompt の主語が最後まで商品/テーマのままだった。
    P2 では **参考shotの映像（reference_visual）を再現する** ことを主文にし、base_prompt は
    その内部で何を映すかを差し替える挿入句として扱う。

    構成（reference_visual が dict のとき）:
        "Recreate reference shot: <visual_desc_en>. Location: <location>. "
        "Framing: <framing_phrase or shot_size composition>. Lighting: <lighting>. "
        "Camera: <camera_phrase>. Palette: <#a #b #c>. Color mood: <color_mood>. "
        "Subject in this shot: <base_prompt>."

    テストとの互換のため、camera_phrase/構図/色調の英語句は従来のものを踏襲する
    （"camera pans to the left" / "medium shot composition" / "warm color mood" 等）。
    reference_visual が空・参考情報が無い shot は base_prompt をそのまま返す（後方互換）。
    """
    if not isinstance(base_prompt, str):
        base_prompt = ""
    rv = shot.get("reference_visual") if isinstance(shot, dict) else None
    if not isinstance(rv, dict):
        return base_prompt

    desc = (rv.get("desc_en") or "").strip()
    location = (rv.get("location") or "").strip()
    framing_raw = (rv.get("framing") or "").strip().lower()
    lighting = (rv.get("lighting") or "").strip()
    color_mood = (rv.get("color_mood") or "").strip().lower()
    shot_size = (rv.get("shot_size") or "").strip().lower()
    cam = (rv.get("camera_move") or "").strip().lower()
    intensity = (rv.get("intensity") or shot.get("motion_intensity") or "").strip().lower()
    palette = rv.get("color_palette_hex") or []
    if not isinstance(palette, list):
        palette = []

    framing_phrase = ""
    if framing_raw in _FRAMING_PHRASES:
        framing_phrase = _FRAMING_PHRASES[framing_raw]
    else:
        if shot_size in ("closeup", "close_up", "close-up"):
            framing_phrase = "close-up composition"
        elif shot_size == "medium":
            framing_phrase = "medium shot composition"
        elif shot_size == "wide":
            framing_phrase = "wide shot composition"

    cam_phrase = _camera_phrase(cam, intensity)

    # base_prompt 側に既にカメラワーク文言があれば重複させない（回帰テスト保護）。
    lowered = base_prompt.lower()
    base_has_camera = any(kw in lowered for kw in ("camera pan", "camera tilt", "camera zoom", "handheld", "dolly"))

    parts: List[str] = []
    if desc:
        parts.append("Recreate reference shot: {}".format(desc.rstrip(". ")))
    if location:
        parts.append("in {}".format(location))
    if framing_phrase:
        parts.append(framing_phrase)
    if lighting:
        # 「natural lighting」「soft daylight lighting」等。既に "lighting" を含むなら重複防止。
        light = lighting if "lighting" in lighting.lower() else "{} lighting".format(lighting)
        parts.append(light)
    if cam_phrase and not base_has_camera:
        parts.append(cam_phrase)
    if palette:
        # プロンプト内では常に小文字 #rrggbb に統一（LLM の色比較を安定させる）。
        clean_palette = [
            c.strip().lower() for c in palette
            if isinstance(c, str) and c.strip().startswith("#")
        ]
        if clean_palette:
            parts.append("palette {}".format(" ".join(clean_palette[:3])))
    if color_mood:
        parts.append("{} color mood".format(color_mood))

    # 参考情報が何も無ければ従来どおり base_prompt そのまま返す（後方互換）。
    if not parts:
        return base_prompt

    subject = base_prompt.strip().rstrip(". ")
    if subject:
        parts.append("Subject in this shot: {}".format(subject))
    return ". ".join(parts) + "."


# Optional 型ヒントを遅延解決するため、モジュール末尾で import する必要はない
# （関数内では文字列としてのみ扱っている）。


# ---------------------------------------------------------------------------
# persona_anchor: 人物 shot の identity 統一（診断 #1/#12 — 話者identity崩壊対策）
# ---------------------------------------------------------------------------
#
# 診断（/tmp/ttp_gap_diagnosis.md）: shot 毎に別 seed 画像を i2v に食わせた結果、
# 前半=男A→後半=男B→女性の手 と話者が別人に切り替わっていた。参考が単一人物の
# リールなら、人物が出る全 shot で「同一人物」を維持すべき。
# 対策（backend 内で完結・sequential generate を前提）:
#   - 最初の人物 shot で生成した動画の代表フレームを抽出し persona 参照画像として保持。
#   - 以降の人物 shot では、その persona 参照を --image-references に前置し、
#     プロンプトへ「same person as the previous shot, consistent identity」を機械追記する。
# 設定 visual.persona_consistency（既定 on）で無効化可能。

# persona 一貫性のためにプロンプト末尾へ機械追記する identity 固定句。
_PERSONA_IDENTITY_PHRASE = (
    "same person as the previous shot, consistent identity, same face, hair and outfit"
)
# 追記済みか判定するための一意マーカー（idempotent 追記のため）。
_PERSONA_IDENTITY_MARKER = "same person as the previous shot"


def _shot_has_person(shot):
    """shot が人物を含むか判定する（reference_visual.has_person もしくは shot.has_person）。"""
    if not isinstance(shot, dict):
        return False
    rv = shot.get("reference_visual")
    if isinstance(rv, dict) and rv.get("has_person") is True:
        return True
    return shot.get("has_person") is True


def _build_persona_prompt(prompt):
    """visual_prompt に identity 固定句を idempotent に追記する（純関数）。

    既に固定句が含まれていれば二重追記しない。空プロンプトでも固定句だけは載せる。
    """
    base = prompt if isinstance(prompt, str) else ""
    if _PERSONA_IDENTITY_MARKER in base.lower():
        return base
    stripped = base.strip().rstrip(". ")
    if not stripped:
        return _PERSONA_IDENTITY_PHRASE + "."
    return "{}. {}.".format(stripped, _PERSONA_IDENTITY_PHRASE)


def _apply_persona_anchor(shot, persona_ref_path):
    """人物 shot に persona 参照画像とidentity固定句を適用した新しい shot dict を返す（純関数・非破壊）。

    - reference_images の先頭に persona_ref_path を前置（重複除去・最大 _MAX_REFERENCE_IMAGES）。
    - visual_prompt に identity 固定句を機械追記。
    persona_ref_path が空なら shot をそのまま返す。
    """
    if not persona_ref_path or not isinstance(shot, dict):
        return shot
    new_shot = dict(shot)
    existing = list(new_shot.get("reference_images") or [])
    merged = [persona_ref_path] + [p for p in existing if p and p != persona_ref_path]
    new_shot["reference_images"] = merged[:_MAX_REFERENCE_IMAGES]
    new_shot["visual_prompt"] = _build_persona_prompt(new_shot.get("visual_prompt", ""))
    return new_shot


# ---------------------------------------------------------------------------
# no_text_in_video: 映像内文字の禁止（①予防層 — AIが描く偽文字/崩れ日本語対策）
# ---------------------------------------------------------------------------
#
# ユーザー報告: Higgsfield実映像の中に文字化け（AIが描いた偽文字・崩れた日本語風の
# 文字）がちょくちょく出る。設計方針①予防: 生成プロンプトに「映像内へ一切文字を
# 描かない」旨のnegative指示を常時注入する。参考ショット自体が文字主体（進捗ゲージ等）
# でも映像には文字を描かせず、テロップ（焼き込み）側でこちらが再現する方針を貫く。
# config visual.no_text_in_video（既定 True・False で従来動作＝注入しない）。
_NO_TEXT_IN_VIDEO_PHRASE = (
    "Do not render any text, letters, words, captions, subtitles, logos, "
    "or watermarks inside the video. All text will be overlaid separately."
)
# idempotent 追記のための一意マーカー（小文字比較。既に含んでいれば二重追記しない）。
_NO_TEXT_MARKER = "do not render any text"


def _append_no_text_directive(prompt, enabled=True):
    """visual_prompt 末尾へ「映像内文字禁止」の negative 指示を idempotent に追記する（純関数）。

    - enabled=False なら prompt をそのまま返す（従来動作）。
    - 既に指示句が含まれていれば二重追記しない。
    - 参考再現の主文・Subject句・persona identity句の全てより後ろ（末尾）に付ける。
      末尾に置くことで、それらの主文と語順・意味の衝突を起こさない。
    """
    if not enabled:
        return prompt
    base = prompt if isinstance(prompt, str) else ""
    if _NO_TEXT_MARKER in base.lower():
        return base
    stripped = base.strip()
    if not stripped:
        return _NO_TEXT_IN_VIDEO_PHRASE
    if stripped.endswith("."):
        return "{} {}".format(stripped, _NO_TEXT_IN_VIDEO_PHRASE)
    return "{}. {}".format(stripped, _NO_TEXT_IN_VIDEO_PHRASE)


# ---------------------------------------------------------------------------
# skin_realism: 素肌リアル化（診断#5・美化禁止）
# ---------------------------------------------------------------------------
#
# 参考(@ami_biyou_ KINUI)は毛穴・産毛・ほくろ・美容液の筋が見える「実素肌」だが、生成は
# プラスチック肌に美化されていた。参考が実素肌のとき（skin_texture=="raw" もしくは人物記述に
# 毛穴/赤み/シミ/産毛等の語がある）だけ、非美化指示＋negative を注入する。参考が綺麗肌
# （skin_texture=="smooth"）のときは注入しない＝あくまで参考準拠を貫く。
# config visual.raw_skin_injection（既定 True）で無効化可能。
_SKIN_KEYWORDS = (
    "pore", "pores", "rough skin", "redness", "blemish", "blemishe", "bare skin",
    "peach fuzz", "skin texture", "wrinkle", "freckle", "acne", "unretouched", "mole",
)
_SKIN_REALISM_PHRASE = (
    "real bare skin with visible pores and natural texture, peach fuzz, "
    "unretouched smartphone footage"
)
_SKIN_REALISM_NEGATIVE = (
    "no airbrushed skin, no plastic smooth skin, no beauty filter, no CGI-perfect skin"
)
# idempotent 追記のための一意マーカー（小文字比較）。
_SKIN_REALISM_MARKER = "real bare skin with visible pores"


def _shot_wants_raw_skin(shot):
    """shot が「実素肌の再現」を必要とするか判定する（純関数・テスト対象）。

    - reference_visual.skin_texture=="smooth" → False（参考が綺麗肌なので美化禁止指示は不要）。
    - skin_texture=="raw" → True。
    - skin_texture 不明 → 人物 shot かつ desc/person_desc/scene_desc に肌の状態語がある場合のみ True。
    """
    if not isinstance(shot, dict):
        return False
    rv = shot.get("reference_visual") if isinstance(shot.get("reference_visual"), dict) else {}
    st = (rv.get("skin_texture") or shot.get("skin_texture") or "").strip().lower()
    if st == "smooth":
        return False
    if st == "raw":
        return True
    if not _shot_has_person(shot):
        return False
    text = " ".join(str(rv.get(k) or "") for k in ("desc_en", "person_desc", "scene_desc_ja"))
    low = text.lower()
    return any(kw in low for kw in _SKIN_KEYWORDS)


def _append_skin_realism_directive(prompt, shot, enabled=True):
    """visual_prompt 末尾へ「実素肌・非美化」の指示＋negative を idempotent に追記する（純関数）。"""
    if not enabled or not _shot_wants_raw_skin(shot):
        return prompt
    base = prompt if isinstance(prompt, str) else ""
    if _SKIN_REALISM_MARKER in base.lower():
        return base
    phrase = "{}. Negative: {}.".format(_SKIN_REALISM_PHRASE, _SKIN_REALISM_NEGATIVE)
    stripped = base.strip()
    if not stripped:
        return phrase
    if stripped.endswith("."):
        return "{} {}".format(stripped, phrase)
    return "{}. {}".format(stripped, phrase)


def _build_extract_frame_cmd(ffmpeg_bin, video_path, out_image_path, at_sec):
    """生成済みクリップから代表フレーム1枚を抽出する ffmpeg コマンドを構築する（副作用なし・テスト対象）。"""
    return [
        str(ffmpeg_bin), "-y", "-ss", "{:.3f}".format(max(0.0, float(at_sec))),
        "-i", str(video_path), "-frames:v", "1", "-q:v", "3", str(out_image_path),
    ]


def _build_create_cmd(cli_bin, model, shot, resolution, no_text_in_video=True, raw_skin_injection=True):
    """ジョブ投入コマンドを構築する（副作用なし・テスト対象）。

    ★generate_audio 等の任意パラメータはCLI側デフォルト（generate_audio=true）に
    委ね、明示的なフラグ指定はしない。フラグ名のkebab/snake表記ゆれを実機未検証の
    まま組み込むリスクを避けるため（--prompt/--aspect-ratio/--resolution/--duration
    の4つのみ実機確認済み)。

    R2b F4: shot に reference_visual があれば camera_move / shot_size / color_mood を
    英語句として visual_prompt 末尾に追加する（Higgsfield 実運用でも参考カメラワークを
    伝えるため）。

    ①予防: no_text_in_video（既定 True）が有効なら、参考再現主文・persona句の後ろ
    （末尾）へ「映像内へ文字を描かない」negative 指示を注入する。
    """
    duration_sec = _resolve_request_duration_sec(shot)
    prompt = _augment_prompt_with_reference_visual(shot.get("visual_prompt", ""), shot)
    # 素肌リアル化: 参考が実素肌のときだけ非美化指示を注入（no_text より前・末尾に載る）。
    prompt = _append_skin_realism_directive(prompt, shot, raw_skin_injection)
    prompt = _append_no_text_directive(prompt, no_text_in_video)
    return [
        cli_bin, "generate", "create", model,
        "--prompt", prompt,
        "--aspect-ratio", "9:16",
        "--resolution", resolution,
        "--duration", str(duration_sec),
    ] + _build_image_args(shot) + ["--json"]


def _build_cost_cmd(cli_bin, model, shot, resolution, no_text_in_video=True, raw_skin_injection=True):
    """コスト見積コマンドを構築する（副作用なし・テスト対象）。

    課金額の見積とジョブ投入(_build_create_cmd)は、画像フラグを含め同じ入力条件で
    行うべき(見積と実際の課金対象が一致する)ため、_build_image_args を共通利用する。
    プロンプトも投入時と同一にするため no_text_in_video の注入も揃える。
    """
    duration_sec = _resolve_request_duration_sec(shot)
    prompt = _augment_prompt_with_reference_visual(shot.get("visual_prompt", ""), shot)
    prompt = _append_skin_realism_directive(prompt, shot, raw_skin_injection)
    prompt = _append_no_text_directive(prompt, no_text_in_video)
    return [
        cli_bin, "generate", "cost", model,
        "--prompt", prompt,
        "--aspect-ratio", "9:16",
        "--resolution", resolution,
        "--duration", str(duration_sec),
    ] + _build_image_args(shot) + ["--json"]


def _build_wait_cmd(cli_bin, job_id, timeout_sec, interval_sec):
    """完了待ちコマンドを構築する（副作用なし・テスト対象）。CLIの --timeout/--interval は Go の duration文字列（例 "600s"）。"""
    return [
        cli_bin, "generate", "wait", job_id,
        "--timeout", "{}s".format(int(timeout_sec)),
        "--interval", "{}s".format(int(interval_sec)),
        "--json",
    ]


def _parse_job_id(stdout_str):
    """createコマンドの標準出力(JSON配列想定)からjob_idを取り出す。"""
    try:
        data = json.loads((stdout_str or "").strip())
    except Exception:
        raise VisualBackendError(
            "higgsfield generate create の応答JSONをパースできませんでした: {!r}".format((stdout_str or "")[:300])
        )
    if isinstance(data, list):
        if not data:
            raise VisualBackendError("higgsfield generate create の応答が空配列でした: {!r}".format(data))
        job_id = data[0]
    elif isinstance(data, dict):
        job_id = data.get("id") or data.get("job_id")
    else:
        job_id = None
    if not job_id:
        raise VisualBackendError("higgsfield generate create の応答からjob_idが取得できません: {!r}".format(data))
    return job_id


def _parse_cost(stdout_str):
    """costコマンドの標準出力(JSON想定)からcredits(int)を取り出す。"""
    try:
        data = json.loads((stdout_str or "").strip())
    except Exception:
        raise VisualBackendError(
            "higgsfield generate cost の応答JSONをパースできませんでした: {!r}".format((stdout_str or "")[:300])
        )
    credits = data.get("credits") if isinstance(data, dict) else None
    if credits is None:
        raise VisualBackendError("higgsfield generate cost の応答からcreditsが取得できません: {!r}".format(data))
    return int(credits)


def _parse_wait_result(stdout_str):
    """waitコマンドの標準出力(JSONオブジェクト想定)を正規化する。

    Returns: {"status": "completed"|"failed", "result_url": str|None, "error": str|None}
    """
    try:
        data = json.loads((stdout_str or "").strip())
    except Exception:
        raise VisualBackendError(
            "higgsfield generate wait の応答JSONをパースできませんでした: {!r}".format((stdout_str or "")[:300])
        )
    raw_status = (data.get("status") or "").lower()
    if raw_status in ("completed", "done", "succeeded", "success"):
        status = "completed"
    elif raw_status in ("failed", "error"):
        status = "failed"
    else:
        status = raw_status or "unknown"
    return {
        "status": status,
        "result_url": data.get("result_url"),
        "error": data.get("error"),
        "raw": data,
    }


def _is_auth_error(stderr_text):
    return "not authenticated" in (stderr_text or "").lower()


# ★クレジット切れ（実機で `not_enough_credits` を含むエラーを返すことを想定・日本語化）:
# 残高不足はユーザー側の対処（チャージ）が必要で自動復旧しないため、transient/nsfw
# いずれのリトライ対象にも当たらないことを保証した上で（_TRANSIENT_ERROR_PATTERNS /
# _SAFETY_REJECTION_MARKERS のどちらにも"not_enough_credits"は一致しない）、
# 即座に分かりやすい日本語エラーへ差し替える。
_CREDIT_ERROR_MARKER = "not_enough_credits"


def _is_credit_error(stderr_text):
    return _CREDIT_ERROR_MARKER in (stderr_text or "").lower()


def _is_timeout_error(stderr_text):
    lowered = (stderr_text or "").lower()
    return "timed out" in lowered or "timeout" in lowered


# ★一時的接続失敗への耐性（実機確認・2026-07-17）: `higgsfield generate create` が
# `Error: request failed (no response received)` という一時的な接続失敗(exit 3)で
# 落ちることがあり、直後にCLIを叩くと正常に通る。14ショット中1回でも起きると全体が
# 失敗していたため、指数バックオフ付きリトライで吸収する。認証エラー
# (`_is_auth_error`)やバリデーションエラー(例: "Input should be")はここに該当せず、
# 再試行しても無駄なので即座に失敗させる。
_TRANSIENT_ERROR_PATTERNS = (
    "no response received",
    "request failed",
    "timed out",
    "timeout",
    "connection",
)

# リトライ回数・バックオフ秒数(5秒→15秒)。要素数=最大リトライ回数。
_DEFAULT_RETRY_BACKOFF_SEC = (5, 15)


def _is_transient_error(text):
    """例外メッセージ(str化したもの)が一時的エラーのパターンに一致するか判定する。"""
    lowered = (text or "").lower()
    return any(pattern in lowered for pattern in _TRANSIENT_ERROR_PATTERNS)


# ★安全フィルタ誤検知への耐性（実機確認・2026-07-17）: 完全に無害なプロンプト
# （例: "bold warning-style graphic with a soft yellow gradient background and a
# simple exclamation icon, clean modern design, vertical 9:16"）で
# `Error: job <uuid> ended with status "nsfw"` が返ることがある(安全フィルタの誤検知)。
# これは_TRANSIENT_ERROR_PATTERNSには一致しない(同一プロンプトの単純再試行は無意味な
# ため、意図的にtransient扱いにしない)。代わりにプロンプトを段階的に安全側へ言い換えて
# create からやり直す専用リトライを generate() 内に持つ。
_SAFETY_REJECTION_MARKERS = ("nsfw", "moderation", "flagged")

# 安全リトライで使う言い換えプロンプト。2回目は元プロンプトに安全側の接頭辞を付加、
# 3回目(最終)は常に安全な抽象的ビジュアルへ完全差し替えする(動画として破綻しない汎用画)。
_SAFETY_RETRY_PREFIX = "wholesome, family-friendly, safe-for-work, "
_SAFETY_RETRY_FALLBACK_PROMPT = (
    "abstract geometric shapes, soft gradient background, clean minimal modern design, vertical 9:16"
)
_MAX_SAFETY_ATTEMPTS = 3


def _is_safety_rejection(text):
    """wait結果のstatus/errorやCLIの非ゼロ終了エラーメッセージが、安全フィルタ(nsfw/moderation/
    flagged)による拒否を示しているか判定する（副作用なし・テスト対象）。

    例: `Error: job <uuid> ended with status "nsfw"` や、statusフィールドそのものが
    "nsfw"/"moderation"/"flagged" のケースの両方をこの1関数でカバーする。
    """
    lowered = (text or "").lower()
    return any(marker in lowered for marker in _SAFETY_REJECTION_MARKERS)


def _build_safety_retry_prompt(original_prompt, attempt):
    """安全フィルタ拒否後の再試行プロンプトを組み立てる（副作用なし・テスト対象）。

    attempt: 1始まりの試行回数。
      1回目: 元プロンプトのまま。
      2回目: 元プロンプトの先頭に安全側の接頭辞を付加。
      3回目(最終): 常に安全な抽象プロンプトへ完全差し替え。
    """
    if attempt <= 1:
        return original_prompt
    if attempt == 2:
        return _SAFETY_RETRY_PREFIX + original_prompt
    return _SAFETY_RETRY_FALLBACK_PROMPT


def _call_with_retry(fn, label, max_retries=2, backoff_sec=_DEFAULT_RETRY_BACKOFF_SEC, sleep_fn=None):
    """fn()を実行し、一時的エラーの場合のみ指数バックオフで最大max_retries回まで再試行する。

    - `HiggsfieldAuthError`（認証切れ）は一時的エラーではないため即座に再送出する（再試行しない）。
    - `HiggsfieldCreditError`（クレジット不足）も例外型で明示的に非リトライとして即座に
      再送出する（メッセージ文字列への `_is_transient_error` 適用には依存しない。日本語
      ラップ後のメッセージにstderr原文が埋め込まれ、そこに偶然"connection"等のtransient
      パターンが含まれるケースでも誤ってリトライされないようにするため）。
    - それ以外の `VisualBackendError` は、メッセージが `_is_transient_error` に一致する場合のみ
      リトライ対象。一致しない場合（バリデーションエラー等）は即座に再送出する。
    - `sleep_fn` はテストでmonkeypatchできるよう外部注入可能にしてある（未指定時は `time.sleep`
      をその都度参照するため、`time.sleep` 自体をmonkeypatchしても効く）。
    """
    attempt = 0
    while True:
        try:
            return fn()
        except HiggsfieldAuthError:
            raise
        except HiggsfieldCreditError:
            raise
        except VisualBackendError as exc:
            if not _is_transient_error(str(exc)):
                raise
            if attempt >= max_retries:
                raise VisualBackendError(
                    "{}が一時的エラーで失敗しました(試行{}回・最大{}回のリトライを含む): {}".format(
                        label, attempt + 1, max_retries, str(exc)[:500]
                    )
                )
            sleep = sleep_fn or time.sleep
            sleep(backoff_sec[min(attempt, len(backoff_sec) - 1)])
            attempt += 1


def _run_cli(cmd, timeout_sec):
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False, timeout=timeout_sec)
    except FileNotFoundError:
        raise VisualBackendError(
            "higgsfield CLIが見つかりません。`npm i -g @higgsfield/cli && higgsfield auth login` を実行してください。"
        )
    except subprocess.TimeoutExpired:
        raise HiggsfieldTimeoutError(
            "higgsfield CLI実行がタイムアウトしました(Python側{}秒超過): {}".format(timeout_sec, " ".join(cmd))
        )
    if proc.returncode != 0:
        stderr_text = proc.stderr.decode("utf-8", "replace")
        if _is_auth_error(stderr_text):
            raise HiggsfieldAuthError(
                "higgsfieldの認証が切れています。`higgsfield auth login` を実行してください。stderr: {}".format(
                    stderr_text[:300]
                )
            )
        if _is_credit_error(stderr_text):
            raise HiggsfieldCreditError(
                "Higgsfieldのクレジットが不足しています。https://higgsfield.ai/ でチャージしてから"
                "「続きから作成する」を押してください。(詳細: {})".format(stderr_text[:300])
            )
        if _is_timeout_error(stderr_text):
            raise HiggsfieldTimeoutError(
                "higgsfield CLIがタイムアウトを報告しました: {}".format(stderr_text[:500])
            )
        raise VisualBackendError(
            "higgsfield CLI実行エラー(exit {}): {}".format(proc.returncode, stderr_text[:500])
        )
    return proc.stdout.decode("utf-8", "replace")


def _download_url(url, out_path, curl_bin=None, timeout_sec=120):
    """result_urlをout_pathへcurlでダウンロードする（副作用あり）。"""
    curl_bin = curl_bin or shutil.which("curl") or "/usr/bin/curl"
    cmd = [curl_bin, "-fsSL", "-o", out_path, url]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False, timeout=timeout_sec)
    except FileNotFoundError:
        raise VisualBackendError("curlコマンドが見つかりません。result_urlのダウンロードができません。")
    except subprocess.TimeoutExpired:
        raise VisualBackendError("result_urlのダウンロードがタイムアウトしました: {}".format(url))
    if proc.returncode != 0:
        raise VisualBackendError(
            "result_urlのダウンロードに失敗しました(exit {}): {}".format(
                proc.returncode, proc.stderr.decode("utf-8", "replace")[:500]
            )
        )


class HiggsfieldBackend(VisualBackend):
    name = "higgsfield"

    def __init__(self, cfg=None):
        super().__init__(cfg)
        full_cfg = cfg or load_config()
        self.hf_cfg = full_cfg.get("higgsfield", {}) or {}
        self.cli_bin = _resolve_cli_bin(self.hf_cfg.get("cli_bin", "higgsfield"))
        self.model = self.hf_cfg.get("model", _DEFAULT_MODEL)
        self.resolution = self.hf_cfg.get("resolution", _DEFAULT_RESOLUTION)
        self.max_credits_per_shot = self.hf_cfg.get("max_credits_per_shot", _DEFAULT_MAX_CREDITS_PER_SHOT)
        self.poll_interval_sec = self.hf_cfg.get("poll_interval_sec", 5)
        self.poll_timeout_sec = self.hf_cfg.get("poll_timeout_sec", 600)
        # persona_anchor: 人物 shot の identity 統一（既定 on）。sequential generate を通じて
        # 最初の人物 shot の代表フレームを保持し、以降の人物 shot の参照へ連鎖させる。
        self.persona_consistency = bool((full_cfg.get("visual") or {}).get("persona_consistency", True))
        # ①予防: 映像内文字の禁止指示を生成プロンプトへ常時注入するか（既定 on）。
        self.no_text_in_video = bool((full_cfg.get("visual") or {}).get("no_text_in_video", True))
        # 素肌リアル化: 参考が実素肌のとき非美化指示を注入するか（既定 on）。
        self.raw_skin_injection = bool((full_cfg.get("visual") or {}).get("raw_skin_injection", True))
        self.ffmpeg_bin = full_cfg.get("ffmpeg_bin") or str(project_root() / "bin" / "ffmpeg")
        self._persona_ref_path = None

    # -- 個別ステップ（テスト容易性のため分割） -----------------------------

    def estimate_cost(self, shot: dict) -> int:
        cmd = _build_cost_cmd(self.cli_bin, self.model, shot, self.resolution, self.no_text_in_video,
                              self.raw_skin_injection)
        stdout = _run_cli(cmd, timeout_sec=60)
        return _parse_cost(stdout)

    def submit_job(self, shot: dict) -> str:
        # ★job_id取得前（=まだジョブが投入できていない/課金が確定していない段階）の
        # 一時的エラーのみをここでリトライする。二重課金防止のため、job_id取得後は
        # このメソッドを呼び直さない（wait側の再試行は wait_for_result 内で完結させる）。
        def _do():
            cmd = _build_create_cmd(self.cli_bin, self.model, shot, self.resolution, self.no_text_in_video,
                                    self.raw_skin_injection)
            stdout = _run_cli(cmd, timeout_sec=60)
            return _parse_job_id(stdout)

        return _call_with_retry(_do, label="higgsfield generate create")

    def wait_for_result(self, job_id: str) -> dict:
        # ★waitのみ再試行（二重課金防止）: createは既に成功しjob_idを取得済みのため、
        # ここで一時的エラーが起きてもcreateをやり直さず、同じjob_idに対するwaitだけを
        # 再試行する。
        def _do():
            cmd = _build_wait_cmd(self.cli_bin, job_id, self.poll_timeout_sec, self.poll_interval_sec)
            # Python側subprocessタイムアウトはCLI自身の--timeoutより余裕を持たせる（CLI側の
            # タイムアウト処理を正としつつ、CLIが応答不能になった場合の保険として使う）。
            stdout = _run_cli(cmd, timeout_sec=self.poll_timeout_sec + 60)
            return _parse_wait_result(stdout)

        return _call_with_retry(_do, label="higgsfield generate wait(job_id={})".format(job_id))

    def fetch_result(self, job_id: str, out_path: str, result_url=None) -> None:
        if not result_url:
            raise VisualBackendError(
                "higgsfield: result_urlが無いためダウンロードできません(job_id={})".format(job_id)
            )
        _download_url(result_url, out_path)

    # ★注意: 基底クラス VisualBackend.poll_job()/run_async_job() は敢えて
    # オーバーライドしない。base.run_async_job() は status=="done" を完了とみなす
    # 別の契約（pending/running/done/failed）を前提にした汎用ポーリングループだが、
    # このバックエンドはCLIの `generate wait` が完了までブロックする方式のため
    # status=="completed" を使う独自フロー(generate()内で直接wait_for_resultを呼ぶ)
    # を使う。もし poll_job を "互換" のつもりでエイリアスすると、base.run_async_job
    # 経由で呼ばれた場合に "completed" が "done" と一致せず完了と判定されないまま
    # タイムアウトするバグになるため、意図的に実装しない
    # （呼べば基底クラスの NotImplementedError で明確に失敗する）。

    # -- 統合フロー --------------------------------------------------------

    def _generate_once(self, shot: dict, out_path: str) -> dict:
        """1回分の生成試行(cost見積→submit→wait→fetch)。

        安全フィルタ拒否(nsfw/moderation/flagged)を検知した場合は
        `HiggsfieldSafetyRejectedError` を送出する(generate()の安全リトライループが捕捉する)。
        それ以外の失敗(認証切れ/コスト超過/通常のジョブ失敗/タイムアウト)は従来どおり
        該当の例外型でそのまま送出する。
        """
        cost = self.estimate_cost(shot)
        if cost > self.max_credits_per_shot:
            raise HiggsfieldCostLimitError(
                "higgsfield: 見積コスト{}クレジットがmax_credits_per_shot({})を超えるため、"
                "ジョブを投入せず中断しました(shot_id={})。config.jsonのhiggsfield.max_credits_per_shot"
                "を見直すか、ショットのduration/解像度を下げてください。".format(
                    cost, self.max_credits_per_shot, shot.get("id")
                )
            )

        try:
            job_id = self.submit_job(shot)
            result = self.wait_for_result(job_id)
        except HiggsfieldAuthError:
            raise
        except VisualBackendError as exc:
            if _is_safety_rejection(str(exc)):
                raise HiggsfieldSafetyRejectedError(str(exc))
            raise

        status = result["status"]

        if status == "failed":
            error_text = result.get("error") or "不明なエラー"
            if _is_safety_rejection(error_text):
                raise HiggsfieldSafetyRejectedError(error_text)
            raise HiggsfieldJobFailedError(
                "higgsfield ジョブ失敗 (job_id={}): {}".format(job_id, error_text)
            )
        if _is_safety_rejection(status):
            raise HiggsfieldSafetyRejectedError("status={}".format(status))
        if status != "completed":
            raise HiggsfieldTimeoutError(
                "higgsfield ジョブが完了ステータスになりませんでした (job_id={}, status={})".format(
                    job_id, status
                )
            )

        self.fetch_result(job_id, out_path, result_url=result.get("result_url"))
        return {
            "backend": self.name,
            "shot_id": shot.get("id"),
            "job_id": job_id,
            "status": status,
            "credits_estimated": cost,
            "result_url": result.get("result_url"),
        }

    def _capture_persona_anchor(self, shot, out_path):
        """生成済みクリップ(out_path)から代表フレームを抽出し persona 参照として保持する。

        best-effort（失敗しても例外は投げず False を返す）。成功時 self._persona_ref_path を
        セットして以降の人物 shot の連鎖に使う。
        """
        try:
            if not out_path or not os.path.isfile(out_path):
                return False
            anchor_path = out_path + ".persona.png"
            duration = float(shot.get("duration_sec", 5) or 5)
            at_sec = max(0.3, min(duration / 2.0, 2.0))
            cmd = _build_extract_frame_cmd(self.ffmpeg_bin, out_path, anchor_path, at_sec)
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False, timeout=30)
            if proc.returncode == 0 and os.path.isfile(anchor_path):
                self._persona_ref_path = anchor_path
                return True
        except Exception:
            pass
        return False

    def generate(self, shot: dict, out_path: str) -> dict:
        # persona_anchor: 人物 shot なら identity 統一を試みる。既に persona 参照が確立して
        # いれば（＝前の人物 shot で代表フレームを抽出済み）、それを参照へ前置しプロンプトへ
        # identity 固定句を追記する。まだ無い（最初の人物 shot）なら、生成成功後に代表フレームを
        # 抽出して以降の連鎖の起点にする。
        person_shot = bool(self.persona_consistency) and _shot_has_person(shot)
        persona_applied = False
        if person_shot and self._persona_ref_path and os.path.isfile(self._persona_ref_path):
            shot = _apply_persona_anchor(shot, self._persona_ref_path)
            persona_applied = True

        # ★image_pathの実在チェックは全リトライ試行に共通の前提条件のため、安全リトライ
        # ループに入る前に1回だけ行う(image_pathは全試行を通じて不変=attempt_shotでも
        # dict(shot)によりそのまま維持される。差し替わるのはvisual_promptのみ)。
        image_path = shot.get("image_path")
        if image_path and not os.path.isfile(image_path):
            raise VisualBackendError("商品画像が見つかりません: {}".format(image_path))

        # ★安全フィルタ誤検知対策(2026-07-17実機確認): nsfw等で拒否されたら、プロンプトを
        # 段階的に安全側へ言い換えてcreateからやり直す。nsfw失敗はクレジット未消課金のため
        # (実測で残高確認済み)、createの再試行は二重課金にならない。
        # ★image_path/reference_imagesは安全リトライでも維持する(言い換えるのはpromptのみ。
        # 3回目の抽象プロンプト差し替え時も商品画像は落とさない=商品画像が主役のため)。
        original_prompt = shot.get("visual_prompt", "")
        last_safety_error = None
        for attempt in range(1, _MAX_SAFETY_ATTEMPTS + 1):
            attempt_shot = dict(shot)
            attempt_shot["visual_prompt"] = _build_safety_retry_prompt(original_prompt, attempt)
            try:
                meta = self._generate_once(attempt_shot, out_path)
            except HiggsfieldSafetyRejectedError as exc:
                last_safety_error = exc
                if attempt < _MAX_SAFETY_ATTEMPTS:
                    continue
                raise VisualBackendError(
                    "映像AIの安全フィルタで拒否されました(誤検知の可能性)。シーンの説明"
                    "(visual_prompt)を変えて再試行してください: {}"
                    "(安全リトライ{}回すべて拒否・最終試行のエラー: {})".format(
                        original_prompt[:80], _MAX_SAFETY_ATTEMPTS, str(last_safety_error)[:300]
                    )
                )
            meta["safety_retry_attempt"] = attempt
            meta["prompt_used"] = attempt_shot["visual_prompt"]
            # persona_anchor の記録・連鎖起点の確立。
            if person_shot:
                if persona_applied:
                    meta["persona_anchor"] = "applied"
                    meta["persona_ref"] = self._persona_ref_path
                else:
                    captured = self._capture_persona_anchor(attempt_shot, out_path)
                    meta["persona_anchor"] = "captured" if captured else "capture_failed"
            else:
                meta["persona_anchor"] = "none"
            return meta
        # 理論上到達しない(ループは必ずreturnかraiseで抜ける)。
        raise VisualBackendError("higgsfield: 安全リトライ処理で予期しない状態になりました。")

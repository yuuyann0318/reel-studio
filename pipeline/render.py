# -*- coding: utf-8 -*-
"""ffmpeg レンダリングコマンド構築（配列で返す）と実行分離。

video-auto-editor/pipeline/render.py のパターン（コマンド構築(build_*)は文字列配列を
返すだけの純関数・実行(run_ffmpeg)は別関数、9:16化・ASS焼込・BGMダッキング・
loudnorm 2パスの考え方）を踏襲。reel向けの違い:
  - 「元動画を切り出す」のではなく「ショットごとに生成済みのクリップを正規化して連結」
  - 音声トラックは各クリップに無く（mock/higgsfieldとも映像のみ生成）、ナレーションwavを
    別入力として合成する（[0:v]=連結映像, [1:a]=ナレーション, [2:a]=BGM(任意)）。

Python 3.9 互換構文のみ。
"""
from __future__ import annotations

import subprocess


def escape_ffmpeg_filter_path(path):
    """ffmpegフィルタグラフのオプション値としてパスを安全に埋め込む（シェルエスケープではない）。"""
    escaped = path.replace("\\", "\\\\").replace("'", "'\\''")
    return "'{}'".format(escaped)


def build_concat_list_content(clip_paths):
    """concat demuxer用 list.txt の中身を生成する。各行 `file '<path>'`。"""
    lines = []
    for p in clip_paths:
        escaped = p.replace("\\", "\\\\").replace("'", "'\\''")
        lines.append("file '{}'".format(escaped))
    return "\n".join(lines) + "\n"


def build_normalize_clip_cmd(ffmpeg_bin, in_path, out_path, duration_sec=None, trim_start=None,
                              pad_to_duration_sec=None, punch_in_filter=None):
    """1ショットクリップを 1080x1920/30fps/yuv420p/音声なし へ正規化するコマンドを構築する。

    ビジュアルバックエンド（mock/higgsfield/cloudapi）が返すクリップの解像度・fpsが
    バックエンドによってまちまちでも、この正規化を経由すればconcat demuxerで安全に
    単純結合できる。duration_sec を指定すると出力尺を明示的に打ち切る
    （バックエンドが指定尺よりわずかに長い/短いクリップを返した場合の吸収）。

    trim_start を指定すると、そこを起点にトリム（Studioのショット編集: trim.start〜trim.end）
    する。ffmpeg の `-ss`（`-i`の前）は高速シークだが、フレーム精度はキーフレーム間隔に
    依存する。本プロジェクトのクリップは短尺（数秒）のmock/higgsfield生成物のため実用上
    十分な精度で足りる。duration_sec には trim後の長さ（end-start）を渡すこと。
    trim_start=None（従来どおり）なら挙動を一切変えない（後方互換）。

    pad_to_duration_sec を指定すると（かつ duration_sec より長い場合）、映像に
    `tpad=stop_mode=clone` を追加し最終フレームを静止延長したうえで、出力尺を
    pad_to_duration_sec まで打ち切る（音声主導タイミング同期モード: 音声断片の実測尺が
    トリム尺より長い場合に、映像を音声に合わせて伸ばすために使う）。
    pad_to_duration_sec が duration_sec 以下、または未指定なら従来どおり duration_sec で
    単純truncateするだけで、この引数を渡さなかった場合と完全に同じコマンドになる（後方互換）。

    punch_in_filter を指定すると（edit enhancement層のパンチイン演出）、正規化フィルタ
    チェーンの末尾に ",{punch_in_filter}" として追加する。None（既定）なら従来どおり
    このフィルタを一切追加しない（後方互換）。build_punch_in_zoompan_filter() が返す
    zoompan文字列を渡すことを想定。
    """
    filt = "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,fps=30,format=yuv420p"
    out_t = duration_sec
    if pad_to_duration_sec and duration_sec and pad_to_duration_sec > duration_sec:
        pad_amount = pad_to_duration_sec - duration_sec
        filt += ",tpad=stop_mode=clone:stop_duration={:.3f}".format(pad_amount)
        out_t = pad_to_duration_sec
    if punch_in_filter:
        filt += "," + punch_in_filter
    cmd = [ffmpeg_bin, "-y"]
    if trim_start:
        cmd += ["-ss", "{:.3f}".format(trim_start)]
    cmd += ["-i", in_path, "-vf", filt, "-an"]
    if out_t:
        cmd += ["-t", "{:.3f}".format(out_t)]
    cmd += ["-c:v", "libx264", "-crf", "18", "-preset", "medium", out_path]
    return cmd


# ---------------------------------------------------------------------------
# 音声主導タイミング同期モード: ショット表示尺の決定・ナレーション断片の配置合成。
# jobs.py（Studio再レンダリング）とrun.py（CLI経路）の両方から共通で使う純関数群。
# ---------------------------------------------------------------------------

SYNC_GAP_SEC = 0.25  # 断片実測尺に足す「間」（無音の余白）
SYNC_MIN_SHOT_SEC = 1.2  # ショット表示尺の下限


def compute_synced_shot_duration(segment_duration_sec):
    """音声断片の実測尺(segment_duration_sec)から、そのショットの表示尺を決める。

    表示尺 = max(断片実測尺 + SYNC_GAP_SEC, SYNC_MIN_SHOT_SEC)。
    テロップ/クリップ尺の両方をこの値で統一することで、音声=テロップ=ショット境界を
    原理的に一致させる。
    """
    return max(float(segment_duration_sec) + SYNC_GAP_SEC, SYNC_MIN_SHOT_SEC)


def resolve_normalize_pad_args(content_duration_sec, target_duration_sec):
    """正規化クリップの元尺(content_duration_sec)を、目標尺(target_duration_sec)へ合わせるための
    build_normalize_clip_cmd 用 (duration_sec, pad_to_duration_sec) を決める。

    target_duration_sec が content_duration_sec より長い場合は映像が足りないのでtpadで埋める
    （duration_sec=content_duration_sec, pad_to_duration_sec=target_duration_sec を返す）。
    target_duration_sec が content_duration_sec 以下（同期不要 or 断片の方が短い）なら単純
    truncateする（duration_sec=target_duration_sec, pad_to_duration_sec=None）。
    target_duration_sec=None（同期モードでない）なら content_duration_sec をそのまま使う
    （従来どおりの単純truncate。呼び出し側の共通処理用）。
    """
    if target_duration_sec is None:
        return content_duration_sec, None
    if target_duration_sec > content_duration_sec:
        return content_duration_sec, target_duration_sec
    return target_duration_sec, None


def build_narration_segments_placement_filters(segments, start_index):
    """ナレーション断片群を、各断片の start_sec 位置にadelay配置するフィルタ断片群を構築する
    （build_sfx_overlay_filtersと同型: adelay+amixのパターンを流用）。

    segments: [{"path","start_sec"}, ...]（呼び出し側が-iで各pathを追加し、
    その入力インデックスがstart_indexから連番で振られている前提）。
    Returns: (filter_parts:list[str], mix_labels:list[str])
    """
    filter_parts = []
    mix_labels = []
    for i, seg in enumerate(segments or []):
        idx = start_index + i
        start_sec = max(0.0, float(seg.get("start_sec", 0.0) or 0.0))
        delay_ms = int(round(start_sec * 1000))
        label = "seg{}".format(i)
        filter_parts.append(
            "[{idx}:a]adelay={delay}|{delay}[{label}]".format(idx=idx, delay=delay_ms, label=label)
        )
        mix_labels.append("[{}]".format(label))
    return filter_parts, mix_labels


def build_narration_segments_concat_cmd(ffmpeg_bin, segments, out_duration, out_path):
    """ナレーション断片群を、各断片の start_sec 位置に配置して1本のwavへ合成する
    （音声主導タイミング同期モード用）。

    各断片はショットの表示区間内で時間的に重ならない設計（=同時に1断片しか音が鳴らない）
    のため、amix(normalize=0)で単純合成しても音量劣化は起きない（build_sfx_overlay_filters/
    build_final_cmdのSFXオーバーレイと同じ考え方）。断片間・末尾はadelay/出力尺打ち切りにより
    自動的に無音になる。out_duration で最終的な尺を打ち切る。
    """
    cmd = [ffmpeg_bin, "-y"]
    for seg in segments or []:
        cmd += ["-i", seg["path"]]

    if not segments:
        # 断片が1つも無い場合の安全側フォールバック（呼び出し側は通常これを使わない想定）。
        cmd += [
            "-f", "lavfi", "-i", "anullsrc=channel_layout=mono:sample_rate=48000",
            "-t", "{:.3f}".format(out_duration or 0.0),
            "-c:a", "pcm_s16le", out_path,
        ]
        return cmd

    filter_parts, mix_labels = build_narration_segments_placement_filters(segments, 0)
    chain = ";".join(filter_parts) + ";" + "".join(mix_labels) + "amix=inputs={}:duration=longest:normalize=0[out]".format(
        len(mix_labels)
    )
    cmd += ["-filter_complex", chain, "-map", "[out]"]
    if out_duration:
        cmd += ["-t", "{:.3f}".format(out_duration)]
    cmd += ["-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le", out_path]
    return cmd


def build_concat_cmd(ffmpeg_bin, list_path, output_path):
    """concat demuxerでクリップ群を単純結合する（再エンコードなし）。"""
    return [ffmpeg_bin, "-y", "-f", "concat", "-safe", "0", "-i", list_path, "-c", "copy", output_path]


def build_loudnorm_filter(measured=None, tp_target=-1.0):
    """loudnorm 2パス用フィルタ文字列を構築する。measured=None なら1パス目(計測のみ)。"""
    if measured:
        return (
            "loudnorm=I=-14:TP={tp}:LRA=11:"
            "measured_I={measured_I}:measured_TP={measured_TP}:measured_LRA={measured_LRA}:"
            "measured_thresh={measured_thresh}:offset={offset}:linear=true:print_format=json"
        ).format(tp=tp_target, **measured)
    return "loudnorm=I=-14:TP={tp}:LRA=11:print_format=json".format(tp=tp_target)


def gain_db_to_linear(gain_db):
    """dB(電力比ではなく振幅比) を ffmpeg volume フィルタ用の線形係数へ変換する。"""
    return 10.0 ** (float(gain_db) / 20.0)


# 外部SFXの mean_volume 差を吸収する正規化ターゲット。ffmpeg volumedetect の
# mean_volume(dB FS) がこの値になるように gain 補正を掛ける。実効的な聴感音量は
# SFX の crest factor（ピーク/平均比）にも依存するため完璧ではないが、
# 「効果音ラボ由来の音源だけ極端に大きい/小さい」を実用範囲まで詰めることが目的。
# BUG-53 修正: 従来 -16dB では最終ミックスで SE がベースライン+3dB を満たさず埋もれていた。
# ミックス経路変更(loudnorm後段でSFXオーバーレイ + BGM/main の SE時刻ダッキング)と合わせて
# -12dB にターゲットを上げる(narration RMS が loudnorm 後で -14dB 付近になるため、SE を
# 3dB 上に載せて 50ms 窓 RMS がベースライン+3dB を安定して超えるようにする)。
SFX_MEAN_VOLUME_TARGET_DB = -12.0

# gain 補正の絶対値上限（±dB）。無音に近い音源への過大ブースト（ノイズ増幅）と、
# インパクト系のピーク過大カットを両側で防ぐ。ブースト側の上限を +18dB へ引き上げる
# (BUG-53: mean_volume=-24 の SFX 素材でも target -12dB 相当に届かせるため。
#  -24 - (-12) = 12dB の adjust では届かず、+16〜+18 のブーストが必要になるケースがある)。
SFX_MEAN_VOLUME_MAX_ADJUST_DB = 18.0


# 合成SFX(whoosh_gen_*.wav 等)は manifest に mean_volume_db を持たないため補正が無効化される。
# 実測で generate_sfx_library.py の合成SFXは mean_volume ≈ -20dB 前後になることが多く、
# BUG-53(SE 埋もれ) の対策として mean_volume 欠損時はこの想定値で補正を進める。
SFX_ASSUMED_MEAN_VOLUME_DB = -20.0


def normalize_sfx_gain_db(base_gain_db, mean_volume_db,
                          target_db=SFX_MEAN_VOLUME_TARGET_DB,
                          max_adjust_db=SFX_MEAN_VOLUME_MAX_ADJUST_DB):
    """SFX の mean_volume(dB) を target に揃える補正量を base_gain_db に足して返す。

    - mean_volume_db が None（manifest 欠損 or ffmpeg 計測失敗）→ SFX_ASSUMED_MEAN_VOLUME_DB
      (-20dB 前後) で補正する(BUG-53: 合成SFXが manifest に mean 未記録のため常に補正なし
      となっていた問題への対処。実測 -19〜-22dB のため -20dB を保守値として採用)。
    - 補正量 = target_db - mean_volume_db を ±max_adjust_db にクランプ。
    - 音源が target より小さい(-24dB など)→ 正の補正でブースト、
      音源が target より大きい(-8dB など)→ 負の補正でカット。
    """
    if mean_volume_db is None:
        mv: float = SFX_ASSUMED_MEAN_VOLUME_DB
    else:
        try:
            mv = float(mean_volume_db)
        except (TypeError, ValueError):
            return float(base_gain_db)
    adjust = float(target_db) - mv
    limit = abs(float(max_adjust_db))
    if adjust > limit:
        adjust = limit
    elif adjust < -limit:
        adjust = -limit
    return float(base_gain_db) + adjust


def build_sfx_overlay_filters(sfx, start_index):
    """SFXオーバーレイ用のフィルタ断片群と、amixでpremixと合流させるラベル配列を構築する（純関数・テスト対象）。

    sfx: [{"path","at_sec","gain_db"[, "mean_volume_db"]}, ...]（呼び出し側が -i で各 path を
    追加し、その入力インデックスが start_index から連番で振られている前提）。
    mean_volume_db がある場合は normalize_sfx_gain_db() で volume 補正を掛けたうえで
    volume= 断片を組み立てる（外部SFXの音量ばらつき吸収）。
    Returns: (filter_parts:list[str], mix_labels:list[str])
    """
    filter_parts = []
    mix_labels = []
    for i, s in enumerate(sfx or []):
        idx = start_index + i
        gain_db = s.get("gain_db", 0.0) or 0.0
        mean_volume_db = s.get("mean_volume_db")
        effective_gain_db = normalize_sfx_gain_db(gain_db, mean_volume_db)
        at_sec = max(0.0, float(s.get("at_sec", 0.0) or 0.0))
        delay_ms = int(round(at_sec * 1000))
        label = "sfx{}".format(i)
        # BUG-53: 外部SFX素材はステレオ(anime_*.mp3 等)だが narration は mono のため、
        # channel_layouts 不一致で amix の出力が意図せず減衰する(実測: SFXが最終ミックスで
        # 事実上消える)。aformat=channel_layouts=mono:sample_rates=48000 を強制して
        # narration の layout に揃える(mono downmix は L/R 平均で -3dB 相当だがそのぶん
        # gain_db 側で吸収する。48kHz は narration/BGM に合わせる)。
        filter_parts.append(
            "[{idx}:a]aformat=channel_layouts=mono:sample_rates=48000,volume={gain:.4f},"
            "adelay={delay}|{delay}[{label}]".format(
                idx=idx, gain=gain_db_to_linear(effective_gain_db), delay=delay_ms, label=label
            )
        )
        mix_labels.append("[{}]".format(label))
    return filter_parts, mix_labels


# ---------------------------------------------------------------------------
# edit enhancement層: assets/profiles/ttp_reference.json の "edit" セクション
# （pipeline.edit_profile.load_edit_profile()）が渡す設定から、プロ編集の演出
# （カット点SE・パンチイン(緩急ズーム)・BGM音量カーブ・フック冒頭のインパクト）用の
# フィルタ文字列/イベント列を構築する純関数群。すべて文字列/リストを返すだけで
# ffmpegを一切呼ばない（テスト対象）。呼び出し側（run.py/jobs.py）は、この層の計算で
# 例外が起きても catch して edit enhancement 無しの従来コマンドへフォールバックすること。
# ---------------------------------------------------------------------------


def compute_cut_sfx_events(shot_boundaries_sec, min_interval_sec):
    """ショット境界秒（各ショットの表示開始時刻。1番目の要素=0.0＝映像の先頭を含む）から、
    「カット」に該当する2番目以降の境界だけを、直前に採用した境界からmin_interval_sec未満
    しか離れていないものを間引きながら抽出する。

    最初のショットの開始（=0.0、映像の先頭）はカットではない（何も切り替わっていない）ため
    常に除外する。境界が1つ以下（ショットが0〜1個）なら空リストを返す。
    """
    events = []
    last = None
    for i, t in enumerate(shot_boundaries_sec or []):
        if i == 0:
            continue
        t = float(t)
        if last is not None and (t - last) < float(min_interval_sec):
            continue
        events.append(t)
        last = t
    return events


def _family_weights_for_event(at_sec, hook_end_sec, cta_start_sec):
    """カット位置(at_sec)がフック直後 / 本編 / CTA前のどこかで、望ましいSFXファミリー
    の重み付けを返す。呼び出し側は pick_cut_sfx にこれを渡す（ビート対応の重み）。

    - フック直後（at_sec <= hook_end_sec + 少しの余裕）: impact 系を最優先
    - CTA前（cta_start_sec 直前・約1秒手前）: riser 系を優先
    - 本編中: transition 系（whoosh/legacy）を中心にたまに pop/shimmer
    """
    # フック直後（フックの終わり付近のカット）: impact を主軸に。
    if hook_end_sec is not None and at_sec <= hook_end_sec + 0.5:
        return {"impact": 5, "whoosh": 2, "legacy": 1}
    # CTA直前（CTA開始の少し前のカット）: riser を主軸に。
    if cta_start_sec is not None and at_sec >= cta_start_sec - 1.5:
        return {"riser": 5, "whoosh": 2, "shimmer": 1}
    # 本編: transition系を中心にたまにアクセント。
    return {"whoosh": 5, "legacy": 2, "pop": 1, "shimmer": 1}


def _filter_events_by_density(events, cut_sfx_cfg, hook_end_sec, cta_start_sec):
    """cut_sfx_cfg["density"] に応じてイベント配列を間引く。

    - "every" / None / 未知: 何もしない（全イベント通過）
    - "every_n": every_n_cuts（既定2）ごとに1つだけ残す
    - "beats_only": hook_end_sec 付近（±0.6秒）と cta_start_sec 直前
      （cta_start-1.5秒以降）のイベントだけ残す。両ヒントが無ければ
      「フック直後（最初のイベント）」と「最後のイベント」の2箇所を残す。
    - "none": すべて捨てる（＝カットSE無し）
    """
    density = (cut_sfx_cfg.get("density") or "every").lower()
    if density in ("every", ""):
        return list(events)
    if density == "none":
        return []
    if density == "every_n":
        n = int(cut_sfx_cfg.get("every_n_cuts") or 2)
        if n <= 1:
            return list(events)
        return [t for i, t in enumerate(events) if i % n == 0]
    if density == "beats_only":
        filtered = []
        for t in events:
            near_hook = hook_end_sec is not None and abs(t - float(hook_end_sec)) < 0.6
            near_cta = cta_start_sec is not None and t >= float(cta_start_sec) - 1.5
            if near_hook or near_cta:
                filtered.append(t)
        if filtered:
            return filtered
        # フォールバック: ヒントが両方無い/どのイベントも境界に近くないなら
        # 「最初 + 最後」の2箇所だけ残す（＝実質フックとCTAの位置）
        if not events:
            return []
        if len(events) == 1:
            return list(events)
        return [events[0], events[-1]]
    return list(events)


def build_edit_cut_sfx_specs(shot_boundaries_sec, edit_profile, project_seed=None,
                              hook_end_sec=None, cta_start_sec=None):
    """edit_profile["cut_sfx"]設定から、build_final_cmdのsfx引数に追加できる
    {"path","at_sec","gain_db"} 群を構築する（純関数）。

    cut_sfx.enabled=False の場合は空リストを返す。

    後方互換の分岐:
      1) cut_sfx.rotate=False (ttp_reference.json で file を明示指定) →
         従来どおり resolved_path 固定でカットSEを全イベントに適用する。
         resolved_path が無い場合は空リスト。
      2) cut_sfx.rotate=True (or 未設定) かつ manifest がある →
         カット位置ごとに pipeline.edit_profile.pick_cut_sfx() で決定論的に
         別ファイルを選び、同一動画内で連続同音を避ける。
         hook_end_sec / cta_start_sec のヒントがあれば位置別の重みで
         impact/riser/transition を切り替える。
      3) rotate=True だが manifest 空 → 従来の resolved_path フォールバック。
         どちらも無ければ空リスト。
    """
    edit_profile = edit_profile or {}
    cfg = edit_profile.get("cut_sfx") or {}
    if not cfg.get("enabled", True):
        return []

    min_interval = cfg.get("min_interval_sec", 1.5)
    gain_db = cfg.get("gain_db", -18)
    events = compute_cut_sfx_events(shot_boundaries_sec, min_interval)
    if not events:
        return []

    # density（レシピが指定するSE密度: every / every_n / beats_only / none）で間引く
    events = _filter_events_by_density(events, cfg, hook_end_sec, cta_start_sec)
    if not events:
        return []

    # first_delay_sec（dramatic レシピ: 「無音→ドン」の遅延スタート）:
    # 最初のイベントだけ後ろへずらして、フック直後の静寂を作る。
    first_delay = float(cfg.get("first_delay_sec") or 0.0)

    # レシピが cut_sfx.family_weights を指定していればそれを常時使う（位置ベースを上書き）。
    # 指定が無ければ従来の位置ベース(_family_weights_for_event)。
    recipe_weights = cfg.get("family_weights")

    rotate = cfg.get("rotate", False)
    manifest = cfg.get("manifest") or []

    if not rotate or not manifest:
        # 固定ファイルモード（後方互換）
        path = cfg.get("resolved_path")
        if not path:
            return []
        return [
            {"path": path, "at_sec": (t + first_delay if i == 0 else t), "gain_db": gain_db}
            for i, t in enumerate(events)
        ]

    # ローテーションモード: 遅延インポートで edit_profile の関数を使う
    # （render.py -> edit_profile.py の依存を導入するが循環はしない）。
    from pipeline import edit_profile as _ep

    specs = []
    last_file = None
    for idx, at_sec in enumerate(events):
        if isinstance(recipe_weights, dict) and recipe_weights:
            weights = recipe_weights
        else:
            weights = _family_weights_for_event(at_sec, hook_end_sec, cta_start_sec)
        picked = _ep.pick_cut_sfx(
            seed=project_seed, index=idx, manifest=manifest,
            family_weights=weights, avoid_file=last_file,
        )
        emit_at = at_sec + first_delay if idx == 0 else at_sec
        if not picked:
            # フォールバック: 固定resolved_pathがあればそれで埋める
            path = cfg.get("resolved_path")
            if not path:
                continue
            specs.append({"path": path, "at_sec": emit_at, "gain_db": gain_db})
            continue
        picked_path = _ep.resolve_sfx_file_path(picked.get("file"))
        if not picked_path:
            continue
        # mean_volume_db は manifest 由来。build_sfx_overlay_filters 側で
        # target -16dB との差分から ±12dB クランプで gain を補正する。
        spec = {"path": picked_path, "at_sec": emit_at, "gain_db": gain_db}
        mean_volume_db = picked.get("mean_volume_db")
        if mean_volume_db is not None:
            spec["mean_volume_db"] = mean_volume_db
        specs.append(spec)
        last_file = picked.get("file")
    return specs


# パンチイン(緩急ズーム)で使うzoompanの出力解像度。正規化後クリップ(1080x1920)と一致させる。
_PUNCH_IN_OUTPUT_SIZE = "1080x1920"


def build_punch_in_zoompan_filter(pattern_name, max_zoom, duration_sec, fps=30):
    """パンチイン(緩急ズーム)用のzoompanフィルタ文字列を1ショット分構築する（純関数）。

    pattern_name:
      - "static": ズームなし。Noneを返す（フィルタ自体を付けない）。
      - "zoom_in_slow": 1.0からmax_zoomまでショット尺いっぱいで均等に緩やかにズームイン。
      - "zoom_in_fast": ショット前半40%で一気にmax_zoomへ立ち上げ、以降はmax_zoomを維持。
      - "pan_subtle": ズームはわずか(1.02とmax_zoomの小さい方)に固定し、横方向にゆっくりパン。
      - 上記以外/未指定: Noneを返す（未知パターンでレンダを壊さないための安全側フォールバック）。

    d=1（1フレームごとにzoom式を評価）・出力fps/解像度は正規化後クリップと合わせる。
    """
    if not pattern_name or pattern_name == "static":
        return None
    total_frames = max(1, int(round(float(duration_sec or 0.0) * fps)))
    max_zoom = float(max_zoom or 1.0)

    if pattern_name == "zoom_in_slow":
        step = (max_zoom - 1.0) / total_frames
        z_expr = "min(zoom+{step:.6f},{maxz:.4f})".format(step=step, maxz=max_zoom)
    elif pattern_name == "zoom_in_fast":
        rise_frames = max(1, int(total_frames * 0.4))
        step = (max_zoom - 1.0) / rise_frames
        z_expr = "min(zoom+{step:.6f},{maxz:.4f})".format(step=step, maxz=max_zoom)
    elif pattern_name == "pan_subtle":
        pan_zoom = min(1.02, max_zoom)
        return (
            "zoompan=z='{z:.4f}':d=1:x='iw/2-(iw/zoom/2)+(on/{total}*20-10)':"
            "y='ih/2-(ih/zoom/2)':s={size}:fps={fps}"
        ).format(z=pan_zoom, total=total_frames, size=_PUNCH_IN_OUTPUT_SIZE, fps=fps)
    else:
        return None

    return (
        "zoompan=z='{z}':d=1:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={size}:fps={fps}"
    ).format(z=z_expr, size=_PUNCH_IN_OUTPUT_SIZE, fps=fps)


def resolve_punch_in_filter_for_shot(shot_index, duration_sec, edit_profile, backend_name):
    """指定ショット(shot_index番目、0始まり)のパンチイン用zoompanフィルタ文字列を解決する。

    以下のいずれかに該当する場合はNone（=フィルタ付与なし）を返す:
      - edit_profile.punch_in.enabled が False
      - backend_name が edit_profile.punch_in.skip_if_backend と一致（既定"mock"。mock
        バックエンドは生成クリップ自体がmotion_presetで疑似ズームを演出済みのため、
        二重にズームをかけないための安全策）
      - pattern（巡回リスト）が空、または該当パターンが"static"/未知
    """
    edit_profile = edit_profile or {}
    cfg = edit_profile.get("punch_in") or {}
    if not cfg.get("enabled", True):
        return None
    skip_backend = cfg.get("skip_if_backend", "mock")
    if skip_backend and backend_name == skip_backend:
        return None
    pattern_list = cfg.get("pattern") or []
    if not pattern_list:
        return None
    pattern_name = pattern_list[shot_index % len(pattern_list)]
    max_zoom = cfg.get("max_zoom", 1.08)
    return build_punch_in_zoompan_filter(pattern_name, max_zoom, duration_sec)


# BGM ダッキング(SE時刻±sfx_dip_half_window_sec で -sfx_dip_db) の既定値。
# BUG-53: SEが最終ミックスで埋もれる問題への追加対策として、SE時刻の窓のみ BGM を
# 追加ダウンさせて SE の可聴性を担保する。
SFX_DIP_HALF_WINDOW_SEC = 0.3
SFX_DIP_GAIN_DB = -4.0
# 本編ミックス全体(loudnorm 後の [main_loud_raw]) を SE 時刻で短時間ダッキングする値。
# BGM だけ -4dB では narration が実測 RMS の大半を占めるため、SE の可聴性が担保できない
# ことが実測で判明した(BUG-53 修正の追加対策)。narration+BGM をまとめて短時間 -6dB 下げ、
# SE の 50ms 窓 RMS がベースライン+3dB を超えるヘッドルームを作る。dip 幅は 0.15s と狭くし
# 過剰ダッキングで narration が体感的に途切れないようにする。
MAIN_DIP_GAIN_DB = -6.0
MAIN_DIP_HALF_WINDOW_SEC = 0.15


def build_main_dip_volume_filter(dip_events, half_window_sec=MAIN_DIP_HALF_WINDOW_SEC,
                                  dip_gain_db=MAIN_DIP_GAIN_DB):
    """[main_loud] を SE 時刻の窓だけ -dip_gain_db ダッキングする volume フィルタ式を返す。

    dip_events: [t_sec, ...]。t_sec ± half_window_sec の窓の間だけ dip_gain_db を掛ける。
    dip_events が空/None なら "volume=1.0" を返す(=何もしない・後方互換)。
    """
    if not dip_events:
        return "volume=1.0"
    dip_lin = gain_db_to_linear(dip_gain_db)
    window_terms = []
    for t in dip_events:
        try:
            tv = float(t)
        except (TypeError, ValueError):
            continue
        if tv < 0:
            tv = 0.0
        lo = max(0.0, tv - float(half_window_sec))
        hi = tv + float(half_window_sec)
        window_terms.append("between(t,{lo:.3f},{hi:.3f})".format(lo=lo, hi=hi))
    if not window_terms:
        return "volume=1.0"
    in_any = "+".join(window_terms)
    expr = "if(gt({in_any},0),{dip:.4f},1)".format(in_any=in_any, dip=dip_lin)
    return "volume='{}':eval=frame".format(expr)


def build_bgm_curve_volume_filter(bgm_curve, fallback_linear):
    """BGM音量カーブ（フック/本編/CTAの三段volume）用のvolumeフィルタ片を構築する。

    bgm_curveがNone/不正（辞書でない）なら、従来どおりの固定音量
    "volume={fallback_linear:.4f}" を返す（後方互換: build_final_cmdにbgm_curveを渡さない
    既存呼び出しは、この関数を経由しても出力コマンド文字列が一切変わらない）。

    bgm_curveありの場合: t（秒）がhook_end_sec未満ならhook音量、cta_start_sec以上なら
    cta音量、それ以外（本編区間）はbody音量、をffmpegのif()式でframe単位に評価する
    （volume=...:eval=frame）。

    さらに bgm_curve["dip_events"] = [t_sec, ...] が指定されていれば、各 t_sec ±
    sfx_dip_half_window_sec の窓の間だけ追加で SFX_DIP_GAIN_DB (=-4dB相当) を掛ける
    (BUG-53: SE埋もれ対策の追加ダッキング)。dip_events が空/未指定なら従来どおり。
    """
    if not isinstance(bgm_curve, dict):
        return "volume={:.4f}".format(fallback_linear)
    hook_end = float(bgm_curve.get("hook_end_sec", 0.0) or 0.0)
    cta_start = float(bgm_curve.get("cta_start_sec", 0.0) or 0.0)
    hook_lin = gain_db_to_linear(bgm_curve.get("hook_gain_db", -10))
    body_lin = gain_db_to_linear(bgm_curve.get("body_gain_db", -14))
    cta_lin = gain_db_to_linear(bgm_curve.get("cta_gain_db", -12))
    base_expr = "if(lt(t,{hook_end:.3f}),{hook:.4f},if(gte(t,{cta_start:.3f}),{cta:.4f},{body:.4f}))".format(
        hook_end=hook_end, cta_start=cta_start, hook=hook_lin, cta=cta_lin, body=body_lin
    )
    dip_events = bgm_curve.get("dip_events") or []
    if dip_events:
        half_w = float(bgm_curve.get("sfx_dip_half_window_sec", SFX_DIP_HALF_WINDOW_SEC))
        dip_db = float(bgm_curve.get("sfx_dip_gain_db", SFX_DIP_GAIN_DB))
        dip_lin = gain_db_to_linear(dip_db)
        # 「いずれかの dip 窓内か?」を or 連鎖で判定し、真なら base_expr に dip_lin を掛ける。
        window_terms = []
        for t in dip_events:
            try:
                tv = float(t)
            except (TypeError, ValueError):
                continue
            if tv < 0:
                tv = 0.0
            lo = max(0.0, tv - half_w)
            hi = tv + half_w
            window_terms.append("between(t,{lo:.3f},{hi:.3f})".format(lo=lo, hi=hi))
        if window_terms:
            # if(A + B + C + ... > 0, dip_lin, 1) を掛ける形で合成
            in_any = "+".join(window_terms)
            full_expr = "({base})*if(gt({in_any},0),{dip:.4f},1)".format(
                base=base_expr, in_any=in_any, dip=dip_lin,
            )
            return "volume='{}':eval=frame".format(full_expr)
    return "volume='{}':eval=frame".format(base_expr)


def build_first_shot_impact_filter(duration_sec=0.3, start_scale=1.04, fps=30):
    """フック1発目のインパクト演出（冒頭duration_sec秒でstart_scale→1.00へスケールポップ）用の
    zoompanフィルタ文字列を構築する。連結後映像([0:v]、字幕焼込前)に適用する想定。
    """
    total_frames = max(1, int(round(float(duration_sec or 0.0) * fps)))
    start_scale = float(start_scale or 1.0)
    delta = (start_scale - 1.0) / total_frames
    z_expr = "if(lte(on,{n}),{start:.4f}-on*({delta:.6f}),1.0)".format(n=total_frames, start=start_scale, delta=delta)
    return (
        "zoompan=z='{z}':d=1:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={size}:fps={fps}"
    ).format(z=z_expr, size=_PUNCH_IN_OUTPUT_SIZE, fps=fps)


def compute_edit_enhancement_kwargs(shot_durations_sec, edit_profile, project_seed=None, plan=None,
                                     reference_spec=None, sfx_timbre_cache=None):
    """editプロファイルと各ショットの表示尺（秒・ショット順のリスト）から、build_final_cmdへ
    渡す追加引数 {"sfx_extra","bgm_curve","first_shot_impact_sec"} をまとめて計算する純関数。

    ショットが0件でも例外を出さず安全な値（sfx_extra=[], bgm_curve=None,
    first_shot_impact_sec=None）を返す。呼び出し側は sfx_extra を既存の sfx リストへ
    追加（+=）し、bgm_curve/first_shot_impact_sec をそのまま build_final_cmd に渡す。

    project_seed（任意）: カットSEをローテーション（cut_sfx.rotate=True）するときの
    決定論シード。プロジェクトごとに異なる seed を渡すと、同じ台本でも別プロジェクトなら
    別のSFX並びになる。None（未指定）でも動作は継続する（同じ並びを再現）。

    plan（任意・後方互換）: pipeline.plan_schema.validate_plan() が返す plan dict。
      - plan.sfx_plan があれば pipeline.sfx_planner.resolve_sfx_events() 経路で
        「映像を意識した」SE配置を採用する（既存の機械配置=compute_cut_sfx_events は
        plan v1 用フォールバックに降格）。shot_durations_sec は plan.shots と同順で
        「実測済みの表示尺」を渡すこと（尺が変わってもSEがズレない）。
      - plan.hook_end_shot_id / plan.cta_start_shot_id があれば
        pipeline.sfx_planner.resolve_hook_cta_bounds() 経路で bgm_curve 境界を
        実測境界から解決する（機械仮定 boundaries[1] / boundaries[-1] はフォールバック）。
      - plan=None なら従来と完全に同じ挙動（後方互換）。
    """
    edit_profile = edit_profile or {}
    durations = [float(d) for d in (shot_durations_sec or [])]

    boundaries = []
    cursor = 0.0
    for d in durations:
        boundaries.append(cursor)
        cursor += d
    total_duration = cursor

    # plan v2 経路: sfx_plan / hook_end_shot_id / cta_start_shot_id を優先。
    plan_has_sfx_plan = isinstance(plan, dict) and bool(plan.get("sfx_plan"))
    plan_hook_end_sec = None
    plan_cta_start_sec = None
    if isinstance(plan, dict) and (plan.get("hook_end_shot_id") or plan.get("cta_start_shot_id")):
        # 遅延importで循環を避ける
        from pipeline import sfx_planner as _sp
        plan_hook_end_sec, plan_cta_start_sec = _sp.resolve_hook_cta_bounds(plan, durations)

    # ビート境界のヒント: plan v2 由来を優先、無ければ機械仮定 boundaries[1]/boundaries[-1]。
    hook_end_hint = plan_hook_end_sec if plan_hook_end_sec is not None else (
        boundaries[1] if len(boundaries) > 1 else None
    )
    cta_start_hint = plan_cta_start_sec if plan_cta_start_sec is not None else (
        boundaries[-1] if durations else None
    )

    if plan_has_sfx_plan and durations:
        # plan v2 経路: sfx_planner で解決
        from pipeline import sfx_planner as _sp
        cut_cfg = edit_profile.get("cut_sfx") or {}
        # sfx_planner は enabled/min_interval を尊重する（enabled=False → 空 sfx）
        if cut_cfg.get("enabled", True):
            min_interval = cut_cfg.get("min_interval_sec", _sp.DEFAULT_MIN_INTERVAL_SEC)
            default_gain = cut_cfg.get("gain_db", _sp.DEFAULT_GAIN_DB)
            manifest = cut_cfg.get("manifest") or []
            # R2b F6: 参考SEの音色 MFCC が使えるとき、family 内で cos 類似度が最も
            # 近いファイルを選ぶ（timbre matching）。reference_spec や cache が未指定
            # ならこの引数は自動で無視され、従来の決定論ローテに戻る（後方互換）。
            ref_sfx_events = None
            ref_duration = None
            if isinstance(reference_spec, dict):
                ref_sfx_events = reference_spec.get("sfx_events") or None
                ref_duration = reference_spec.get("duration_sec")
            sfx_extra = _sp.resolve_sfx_events(
                plan, durations,
                edit_profile=edit_profile,
                manifest=manifest,
                project_seed=project_seed,
                min_interval_sec=min_interval,
                default_gain_db=default_gain,
                reference_sfx_events=ref_sfx_events,
                reference_duration_sec=ref_duration,
                sfx_timbre_cache=sfx_timbre_cache,
            )
        else:
            sfx_extra = []
    else:
        # plan v1 / plan 未指定: 従来経路（compute_cut_sfx_events 経由）
        sfx_extra = build_edit_cut_sfx_specs(
            boundaries, edit_profile,
            project_seed=project_seed,
            hook_end_sec=hook_end_hint,
            cta_start_sec=cta_start_hint,
        ) if durations else []

    bgm_curve = None
    bgm_cfg = edit_profile.get("bgm_curve") or {}
    if durations and bgm_cfg.get("enabled", True):
        hook_end = hook_end_hint if hook_end_hint is not None else total_duration
        cta_start = cta_start_hint if cta_start_hint is not None else total_duration
        bgm_curve = {
            "hook_end_sec": hook_end,
            "cta_start_sec": cta_start,
            "hook_gain_db": bgm_cfg.get("hook_gain_db", -10),
            "body_gain_db": bgm_cfg.get("body_gain_db", -14),
            "cta_gain_db": bgm_cfg.get("cta_gain_db", -12),
            "fade_out_sec": bgm_cfg.get("fade_out_sec", 1.5),
        }
        # BUG-53: SE時刻±0.3sで-4dBダッキング。sfx_extra の at_sec を dip_events に写す
        # (plan.sfx_plan 経路・plan v1 経路の両方をカバー)。edit_profile 側で
        # bgm_curve.sfx_dip_enabled=False を指定した場合はこの処理をスキップする。
        if bgm_cfg.get("sfx_dip_enabled", True) and sfx_extra:
            dip_events = []
            for s in sfx_extra:
                try:
                    dip_events.append(float(s.get("at_sec", 0.0) or 0.0))
                except (TypeError, ValueError):
                    continue
            if dip_events:
                bgm_curve["dip_events"] = dip_events
                if "sfx_dip_half_window_sec" in bgm_cfg:
                    bgm_curve["sfx_dip_half_window_sec"] = float(bgm_cfg["sfx_dip_half_window_sec"])
                if "sfx_dip_gain_db" in bgm_cfg:
                    bgm_curve["sfx_dip_gain_db"] = float(bgm_cfg["sfx_dip_gain_db"])

    first_shot_impact_sec = None
    impact_cfg = edit_profile.get("first_shot_impact") or {}
    if durations and impact_cfg.get("enabled", True) and impact_cfg.get("scale_pop", True):
        first_shot_impact_sec = min(0.3, durations[0])

    # BUG-53: main mix のダッキング窓時刻 = SE の at_sec 列。build_final_cmd の
    # main_dip_events 引数へ渡すことで、SE の 50ms 窓 RMS がベースライン+3dB を超える
    # ヘッドルームを作る。sfx_extra が空なら main_dip_events も空。
    main_dip_events = [float(s.get("at_sec") or 0.0) for s in (sfx_extra or [])]

    return {
        "sfx_extra": sfx_extra,
        "bgm_curve": bgm_curve,
        "first_shot_impact_sec": first_shot_impact_sec,
        "main_dip_events": main_dip_events,
    }


def build_final_cmd(ffmpeg_bin, concat_video_path, narration_wav_path, output_path, ass_path, fonts_dir,
                     bgm_path=None, out_duration=None, loudnorm_measured=None,
                     audio_prefilter=None, tp_target=-1.0, bgm_gain_db=None, sfx=None, ducking=True,
                     bgm_curve=None, first_shot_impact_sec=None, main_dip_events=None):
    """字幕焼き込み＋ナレーション＋BGMダッキング＋SFXオーバーレイ＋loudnorm＋最終エンコード。

    入力: [0]=連結済み映像(音声なし), [1]=ナレーションwav, [2]=BGM(任意),
    続く入力=sfx(任意・複数可)。
    bgm_path=None ならBGM工程を丸ごとスキップ（BGM無しでも必ず完成させる）。
    out_duration は必須（ナレーションが映像より短い場合に無音で末尾を埋め、
    映像尺と音声尺を必ず一致させるため apad=whole_dur に使う）。
    bgm_gain_db: BGM音量(dB)。Noneなら従来どおりvolume=0.55相当（後方互換）。
    ducking: Trueなら従来どおりナレーション区間でBGMをsidechaincompressで抑える。
    Falseならダッキングせず単純にamixする（plan.bgm.ducking=falseを反映するため）。
    sfx: [{"path","at_sec","gain_db"}, ...]。各SFXをat_secの位置にamixでオーバーレイする
    （出力尺は-tで最終的に打ち切るため変わらない）。
    bgm_curve: {"hook_end_sec","cta_start_sec","hook_gain_db","body_gain_db","cta_gain_db",
    "fade_out_sec"}（pipeline.render.compute_edit_enhancement_kwargs()が構築）。Noneなら
    従来どおりbgm_gain_db基準の固定音量・fade 2秒（後方互換）。
    first_shot_impact_sec: 指定すると連結映像([0:v]、字幕焼込の直前)冒頭にスケールポップの
    zoompanを適用する（build_first_shot_impact_filter）。Noneなら従来どおり何も付与しない
    （後方互換）。
    """
    subtitles_filter = "subtitles=filename={}:fontsdir={}".format(
        escape_ffmpeg_filter_path(ass_path), escape_ffmpeg_filter_path(fonts_dir)
    )
    loudnorm_filter = build_loudnorm_filter(loudnorm_measured, tp_target=tp_target)
    prefilter = (audio_prefilter + ",") if audio_prefilter else ""
    dur = out_duration if out_duration else 0.0
    bgm_volume = gain_db_to_linear(bgm_gain_db) if bgm_gain_db is not None else 0.55
    bgm_volume_filter = build_bgm_curve_volume_filter(bgm_curve, bgm_volume)
    fade_out_sec = float(bgm_curve.get("fade_out_sec", 2.0)) if isinstance(bgm_curve, dict) else 2.0
    impact_prefix = (build_first_shot_impact_filter(first_shot_impact_sec) + ",") if first_shot_impact_sec else ""

    cmd = [ffmpeg_bin, "-y", "-i", concat_video_path, "-i", narration_wav_path]
    next_index = 2

    # BUG-53: ミックス経路を再構成。
    #   - loudnorm 2パスは本編ミックス(ナレーション+BGM)のみに掛ける([main_mix])。
    #   - SFXオーバーレイは loudnorm の後段で乗せる(=ラウドネス平坦化でSEのピークが潰されない)。
    #   - SFX重畳後は alimiter でクリップ防止(SEオーバーレイは normalize=0 で合算するため)。
    # BGM/ナレのミックス(=[main_mix]) を組み立てる chain 断片
    if bgm_path:
        cmd += ["-i", bgm_path]
        bgm_index = next_index
        next_index += 1
        fade_st = max(dur - fade_out_sec, 0.0)
        if ducking:
            main_chain = (
                "[0:v]{impact}{subs}[vout];"
                "[{bgm_idx}:a]aloop=loop=-1:size=2e9,atrim=0:{dur:.3f},afade=t=out:st={fade_st:.3f}:d={fade_out:.3f},{bgm_vol_filter}[bgm];"
                "[1:a]{pre}apad=whole_dur={dur:.3f},asplit[voice][sc];"
                "[bgm][sc]sidechaincompress=threshold=0.02:ratio=10:attack=20:release=500:makeup=1[duck];"
                "[voice][duck]amix=inputs=2:duration=longest:normalize=0[main_mix]"
            ).format(impact=impact_prefix, subs=subtitles_filter, dur=dur, fade_st=fade_st, fade_out=fade_out_sec,
                     pre=prefilter, bgm_idx=bgm_index, bgm_vol_filter=bgm_volume_filter)
        else:
            main_chain = (
                "[0:v]{impact}{subs}[vout];"
                "[{bgm_idx}:a]aloop=loop=-1:size=2e9,atrim=0:{dur:.3f},afade=t=out:st={fade_st:.3f}:d={fade_out:.3f},{bgm_vol_filter}[bgm];"
                "[1:a]{pre}apad=whole_dur={dur:.3f}[voice];"
                "[voice][bgm]amix=inputs=2:duration=longest:normalize=0[main_mix]"
            ).format(impact=impact_prefix, subs=subtitles_filter, dur=dur, fade_st=fade_st, fade_out=fade_out_sec,
                     pre=prefilter, bgm_idx=bgm_index, bgm_vol_filter=bgm_volume_filter)
    else:
        main_chain = "[0:v]{impact}{subs}[vout];[1:a]{pre}apad=whole_dur={dur:.3f}[main_mix]".format(
            impact=impact_prefix, subs=subtitles_filter, dur=dur, pre=prefilter
        )

    sfx_filter_parts, sfx_labels = build_sfx_overlay_filters(sfx, next_index)
    for s in (sfx or []):
        cmd += ["-i", s["path"]]
    next_index += len(sfx or [])

    # loudnorm を [main_mix] に適用 → [main_loud_raw]
    chain = main_chain + ";[main_mix]{loud}[main_loud_raw]".format(loud=loudnorm_filter)

    if sfx_labels:
        # BUG-53: SE時刻の窓だけ [main_loud_raw] を短時間ダッキングして SE ヘッドルームを作る。
        # main_dip_events が None/空なら "volume=1.0" で完全パススルー(後方互換)。
        # dip 深さは既定 MAIN_DIP_GAIN_DB(-3dB, 実測で narration RMS を過剰に潰さない値)。
        dip_filter = build_main_dip_volume_filter(main_dip_events)
        chain += ";[main_loud_raw]{dip}[main_loud]".format(dip=dip_filter)
        # SFX を loudnorm 後段で重畳 → [final_mix] → 軽い hard cap → [aout]
        chain += ";" + ";".join(sfx_filter_parts)
        mix_inputs = "[main_loud]" + "".join(sfx_labels)
        chain += ";{mix_inputs}amix=inputs={n}:duration=longest:normalize=0[final_mix]".format(
            mix_inputs=mix_inputs, n=1 + len(sfx_labels)
        )
        # BUG-53: alimiter(attack/release付き)は SE の transient を release=100ms のあいだ
        # 潰し続けて 50ms RMS 窓のスパイクを平坦化するため使わない。asoftclip=type=tanh で
        # ピークだけを瞬間的に丸めるだけにする(RMS には影響しない)。
        chain += ";[final_mix]asoftclip=type=tanh:threshold=0.94[aout]"
    else:
        chain += ";[main_loud_raw]anull[aout]"

    cmd += ["-filter_complex", chain, "-map", "[vout]", "-map", "[aout]"]

    if out_duration:
        cmd += ["-t", "{:.3f}".format(out_duration)]

    cmd += [
        "-c:v", "libx264", "-profile:v", "high", "-pix_fmt", "yuv420p", "-r", "30",
        "-crf", "18", "-preset", "slow",
        "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart",
        output_path,
    ]
    return cmd


def run_ffmpeg(cmd, timeout_sec=None):
    """build_*_cmd が返したコマンド配列を実行する。shell=False。"""
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        timeout=timeout_sec,
    )
    return {
        "returncode": proc.returncode,
        "stdout": proc.stdout.decode("utf-8", "replace"),
        "stderr": proc.stderr.decode("utf-8", "replace"),
    }

# -*- coding: utf-8 -*-
"""編集プロファイル（assets/profiles/ttp_reference.json の "edit" セクション）のロード。

プロ編集の要素（カット点のSE・パンチイン(緩急ズーム)・BGM音量カーブ・フック冒頭の
インパクト）をJSONから読み込み、render.py の filtergraph 構築へ渡す既定値付き辞書を返す。
ttp_reference.json 自体が存在しない/壊れている/"edit"セクションが無い場合も、
pipeline.config.load_config() と同じ「デフォルトで補完して動作継続する」方針で
安全にフォールバックする。

Python 3.9 互換構文のみ。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pipeline.config import project_root

_TTP_REFERENCE_PATH = project_root() / "assets" / "profiles" / "ttp_reference.json"
_SFX_MANIFEST_PATH = project_root() / "assets" / "sfx" / "manifest.json"
_SFX_DIR = project_root() / "assets" / "sfx"

# assets/sfx/manifest.json の label_jp にこの文字列を含むエントリを
# 「カット点の場面転換SE」の既定として選ぶ（whoosh_01.wav = 「ホワッシュ（場面転換）」）。
_TRANSITION_LABEL_HINT = "場面転換"
_FALLBACK_CUT_SFX_FILE = "whoosh_01.wav"

# pick_cut_sfx の既定 family_weights（transition 系を主軸に、たまに accent を混ぜる）。
# 呼び出し側が None を渡した場合や、位置ヒントが無い場合はこれを使う。
DEFAULT_FAMILY_WEIGHTS = {
    "whoosh": 5,
    "legacy": 2,   # 手作りの旧SFX（whoosh_01/swoosh_01等）も混ぜる
    "pop": 1,
    "shimmer": 1,
}

DEFAULT_EDIT_PROFILE = {
    "cut_sfx": {
        "enabled": True,
        "file": None,  # None -> _default_cut_sfx_file() で manifest から解決する
        "gain_db": -18,
        "min_interval_sec": 1.5,
    },
    "punch_in": {
        "enabled": True,
        "pattern": ["zoom_in_slow", "static", "zoom_in_fast", "pan_subtle"],
        "max_zoom": 1.08,
        "skip_if_backend": "mock",
    },
    "bgm_curve": {
        "enabled": True,
        "hook_gain_db": -10,
        "body_gain_db": -14,
        "cta_gain_db": -12,
        "fade_out_sec": 1.5,
    },
    "first_shot_impact": {
        "enabled": True,
        "scale_pop": True,
    },
}


def _load_manifest_raw():
    """assets/sfx/manifest.json を読んでリストで返す。読めない/形式不正なら []。"""
    try:
        manifest = json.loads(_SFX_MANIFEST_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(manifest, list):
        return []
    return manifest


def load_sfx_manifest():
    """assets/sfx/manifest.json を正規化して返す。

    各エントリは少なくとも file/family/tags/duration を持つ dict。
    family が無い旧形式（generate_placeholder_sfx.py 由来）は family="legacy" として補う
    （後方互換: manifest 形式が新旧混在しても壊れない）。
    """
    normalized = []
    for entry in _load_manifest_raw():
        if not isinstance(entry, dict):
            continue
        file_name = entry.get("file")
        if not file_name:
            continue
        family = entry.get("family") or "legacy"
        tags = entry.get("tags")
        if not isinstance(tags, list):
            tags = []
        normalized.append({
            "file": file_name,
            "label_jp": entry.get("label_jp"),
            "family": family,
            "tags": list(tags),
            "duration": entry.get("duration"),
        })
    return normalized


def _default_cut_sfx_file():
    """assets/sfx/manifest.json から「場面転換」系のSFXファイル名を選ぶ。

    manifestが読めない/該当ラベルが無い場合は既知の既定ファイル名にフォールバックする
    （SFX一式の差し替え・並べ替えでmanifest構造が変わっても壊れないようにするため）。
    """
    try:
        manifest = json.loads(_SFX_MANIFEST_PATH.read_text(encoding="utf-8"))
    except Exception:
        return _FALLBACK_CUT_SFX_FILE
    if not isinstance(manifest, list):
        return _FALLBACK_CUT_SFX_FILE
    for entry in manifest:
        if not isinstance(entry, dict):
            continue
        if _TRANSITION_LABEL_HINT in (entry.get("label_jp") or ""):
            f = entry.get("file")
            if f:
                return f
    return _FALLBACK_CUT_SFX_FILE


def _deep_copy_defaults():
    return json.loads(json.dumps(DEFAULT_EDIT_PROFILE))


def _merge_section(defaults_section, override_section):
    """1セクション分（例: "cut_sfx"）を、defaultsをベースにoverrideのキーだけ上書きしてマージする。
    override_sectionが辞書でなければdefaults_sectionをそのまま返す（壊れた入力への耐性）。
    """
    if not isinstance(override_section, dict):
        return dict(defaults_section)
    merged = dict(defaults_section)
    merged.update(override_section)
    return merged


def _merge_edit_profile(overrides):
    defaults = _deep_copy_defaults()
    defaults["cut_sfx"]["file"] = _default_cut_sfx_file()

    if not isinstance(overrides, dict):
        overrides = {}

    merged = {}
    for section_name, section_defaults in defaults.items():
        merged[section_name] = _merge_section(section_defaults, overrides.get(section_name))
    return merged


def _resolve_cut_sfx_path(cut_sfx):
    """cut_sfx.file（相対ファイル名）を assets/sfx/ 配下の実パスへ解決する。
    ファイルが存在しなければ None（呼び出し側はNoneならカットSEをスキップする）。
    """
    return resolve_sfx_file_path(cut_sfx.get("file"))


def resolve_sfx_file_path(file_name):
    """SFXの相対ファイル名を assets/sfx/ 配下の実パスへ解決する。存在しなければ None。"""
    if not file_name:
        return None
    p = Path(file_name)
    resolved = p if p.is_absolute() else (_SFX_DIR / file_name)
    return str(resolved) if resolved.exists() else None


def _seed_int(seed):
    """任意の seed（str/int/None）を安定整数に変換する（決定論のため）。"""
    if seed is None:
        return 0
    if isinstance(seed, int):
        return seed
    digest = hashlib.md5(str(seed).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=False)


def _rand01(seed_int, index):
    """seed_int と index から [0.0, 1.0) の擬似乱数を1つ決定論的に返す。

    hashlib.md5 で計算するため Python バージョン間で挙動が変わらない
    （random.Random は Python 実装差の影響を受けにくいが、より確実にするため）。
    """
    key = "{}:{}".format(seed_int, index).encode("utf-8")
    digest = hashlib.md5(key).digest()
    n = int.from_bytes(digest[:8], "big", signed=False)
    return (n % 10 ** 12) / float(10 ** 12)


def pick_cut_sfx(seed, index, manifest, family_weights=None, avoid_file=None):
    """カット点SEを決定論的に1件選ぶ。

    - seed: プロジェクト単位のシード。同じ seed なら同じ index に対して同じ結果を返す。
    - index: そのプロジェクト内でのカットSEイベント番号（0始まり）。
    - manifest: load_sfx_manifest() が返す正規化済み manifest リスト
                （各要素は少なくとも {"file","family","tags"} を持つ dict）。
    - family_weights: {"family_name": 重み(int)} の辞書。
                      Noneなら DEFAULT_FAMILY_WEIGHTS を使う。
                      重みが正のファミリーだけが候補になる。
    - avoid_file: 直前に採用した file 名（同一動画内の連続同音を避けるため）。
                  同じ file が選ばれた場合は、決定論的にリストの次のエントリへずらす。

    Returns: 選ばれたエントリ dict、候補が1つも無ければ None。
    """
    if not manifest:
        return None

    weights = family_weights if family_weights else DEFAULT_FAMILY_WEIGHTS
    # 候補ファミリー（重み>0 かつ manifest に実在するもの）を優先順で列挙。
    families_by_pref = sorted(
        [(fam, w) for fam, w in weights.items() if w > 0],
        key=lambda kv: (-kv[1], kv[0]),
    )

    # まず「重み付き」でファミリーを1つ選ぶ。
    seed_int = _seed_int(seed)
    total_weight = sum(w for _, w in families_by_pref)
    picked_family = None
    if total_weight > 0:
        r = _rand01(seed_int, index) * total_weight
        cumulative = 0.0
        for fam, w in families_by_pref:
            cumulative += w
            if r < cumulative:
                picked_family = fam
                break

    def _entries_for(fam):
        return [e for e in manifest if e.get("family") == fam]

    # ファミリー内で決定論的に1件選ぶ。無ければ他のファミリーへフォールバック。
    tried_families = []
    fam_order = ([picked_family] if picked_family else []) + [
        fam for fam, _ in families_by_pref if fam != picked_family
    ]
    # 最後の砦: どのファミリーにも該当が無ければ manifest 全体から選ぶ
    fam_order.append(None)

    last_resort = None  # 全ファミリー全滅時の最後の保険（先頭候補）
    for fam in fam_order:
        if fam in tried_families:
            continue
        tried_families.append(fam)
        candidates = _entries_for(fam) if fam is not None else list(manifest)
        if not candidates:
            continue
        # 決定論オフセット
        offset = int(_rand01(seed_int, index * 7919 + 13) * len(candidates))
        for k in range(len(candidates)):
            entry = candidates[(offset + k) % len(candidates)]
            if avoid_file and entry.get("file") == avoid_file:
                continue
            return entry
        # このファミリー内は avoid_file と全衝突 → 次ファミリーへフォールバック
        # （最後の保険用に先頭だけ覚えておく）
        if last_resort is None:
            last_resort = candidates[0]

    # どのファミリーでも avoid_file を回避できなかった場合の最後の保険
    return last_resort


def load_edit_profile(cfg=None):
    """ttp_reference.json の "edit" セクションを読み、既定値で補完して返す。

    cfg引数は他のローダー（load_config等）とインターフェースを揃えるために受け取るが、
    現状は未使用（将来プロジェクトごとの上書きに拡張する余地を残すため）。
    戻り値の各section dictには "resolved_path"（cut_sfxのみ・実パスまたはNone）を追加する。
    """
    raw_edit = None
    try:
        data = json.loads(_TTP_REFERENCE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            raw_edit = data.get("edit")
    except Exception:
        raw_edit = None

    merged = _merge_edit_profile(raw_edit)
    merged["cut_sfx"]["resolved_path"] = _resolve_cut_sfx_path(merged["cut_sfx"])

    # 新規: カットSEローテーション用の manifest とフラグを添付する。
    # ttp_reference.json の edit.cut_sfx.file が明示指定された場合は「固定ファイル」モード
    # (rotate=False) として従来どおり resolved_path を使う（後方互換）。
    # 明示指定が無ければ、manifest 全体からファミリー重みで毎カット選ぶローテーションモード。
    user_specified_file = False
    if isinstance(raw_edit, dict):
        cut_sfx_override = raw_edit.get("cut_sfx")
        if isinstance(cut_sfx_override, dict) and cut_sfx_override.get("file"):
            user_specified_file = True

    merged["cut_sfx"]["manifest"] = load_sfx_manifest()
    merged["cut_sfx"]["rotate"] = not user_specified_file
    return merged

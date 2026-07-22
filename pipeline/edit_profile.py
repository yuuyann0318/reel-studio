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
_EDIT_RECIPES_PATH = project_root() / "assets" / "profiles" / "edit_recipes.json"

# 外部実物SFX(source=="external") と 合成SFX(gen系) の重み比。カット点で
# 同じ family の候補が両方いる場合、外部を「主役」として 80%、合成を「変化球」として
# 20% の確率で選ぶ。effect_dna が同じになる合成音の連続を避けるための調整。
EXTERNAL_SOURCE_WEIGHT = 4  # 4 / (4+1) = 80%
NON_EXTERNAL_SOURCE_WEIGHT = 1

# "long" タグ (assets/sfx/integrate_external.py が 2.5秒超の音源に付与) は
# カットSEローテからは除外する（カットSE=短音のみ）。フックのriserや長尺
# 演出音を将来別ローテで使えるよう「消す」のではなく「タグで隠す」設計。
LONG_TAG = "long"

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
        # BUG-53: SEが最終ミックスで埋もれる問題への対策として -18 → -6dB(実聴感 -10〜-6dB目安)。
        # -12dB target への mean_volume 補正(最大 ±18dB)と合わせて、SFX の 50ms 窓 RMS が
        # ベースライン(narration+BGM)+3dB を安定して超えるようにする。
        "gain_db": -6,
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
        # source は外部実物SFX(=external/*.mp3)にのみ "external" を明示する。
        # 旧エントリ（source キーが無い合成SFX・legacy）は None のままにして、
        # pick_cut_sfx 側で "外部優先の重み" を非破壊的に扱えるようにする。
        source = entry.get("source")
        if source is None and isinstance(file_name, str) and file_name.startswith("external/"):
            # manifest 未更新の環境でも file パスから外部音源を推定できるようにする
            # （integrate_external.py を通した直後は source が入っているが、
            #  古い manifest から起動する場合の安全網）。
            source = "external"
        # mean_volume_db (integrate_external.py が保存) は SFX 配置時に
        # target -16dB との差分で音量補正するために propagate する。
        # manifest 未更新環境（キー欠損）でも None のまま通す（補正なし挙動）。
        mean_volume_db = entry.get("mean_volume_db")
        if mean_volume_db is not None:
            try:
                mean_volume_db = float(mean_volume_db)
            except (TypeError, ValueError):
                mean_volume_db = None
        normalized.append({
            "file": file_name,
            "label_jp": entry.get("label_jp"),
            "family": family,
            "tags": list(tags),
            "duration": entry.get("duration"),
            "source": source,
            "mean_volume_db": mean_volume_db,
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


def _is_long(entry):
    """カットSEローテ対象外（>2.5秒などの長尺）判定。tags に LONG_TAG を含むか。"""
    return LONG_TAG in (entry.get("tags") or [])


def _filter_cut_sfx_candidates(entries):
    """カットSE候補として使える manifest エントリだけを残す。

    除外条件:
      - LONG_TAG を持つ長尺エントリ（>2.5秒。短音カットSEに向かないため）。
      - family=="texture" のエントリ（環境音・演出音・BGM素材で、カット効果音
        としては長すぎたり単体でSE用に切り出せない音源が多いため一律除外）。
    """
    return [
        e for e in entries
        if not _is_long(e) and (e.get("family") or "") != "texture"
    ]


def _source_weight_for(entry):
    """カット点SE選択時の候補エントリごとの重み。外部実物SFX(source=="external")を
    合成SFX(gen/legacy/None)より優先する（EXTERNAL_SOURCE_WEIGHT / NON_EXTERNAL_SOURCE_WEIGHT）。

    「音色のDNAが合成音で同じ」問題への対策。同じ family 内で外部と合成が
    共存するときだけ効き、片方しか候補が無ければどの音が選ばれるかは変わらない。
    """
    if (entry.get("source") or "").lower() == "external":
        return EXTERNAL_SOURCE_WEIGHT
    return NON_EXTERNAL_SOURCE_WEIGHT


def _weighted_pick_from_candidates(candidates, seed_int, index, avoid_file):
    """候補配列から source 重みで1件選ぶ（決定論）。

    - avoid_file と同一 file のエントリは重み0扱いで除外する
      （candidates 全部が avoid_file と一致する場合は None を返す）。
    - すべての source 重みが0（=候補全滅）なら None を返す。
    """
    weighted = []
    for e in candidates:
        if avoid_file and e.get("file") == avoid_file:
            continue
        weighted.append((e, _source_weight_for(e)))
    total = sum(w for _, w in weighted)
    if total <= 0:
        return None
    # 決定論オフセット: index * 7919 + 13 のシフトは従来と同一
    # （seed/index が同じなら同じ結果になることを保証するため）
    r = _rand01(seed_int, index * 7919 + 13) * total
    cumulative = 0.0
    for entry, w in weighted:
        cumulative += w
        if r < cumulative:
            return entry
    return weighted[-1][0]


def pick_cut_sfx(seed, index, manifest, family_weights=None, avoid_file=None):
    """カット点SEを決定論的に1件選ぶ。

    - seed: プロジェクト単位のシード。同じ seed なら同じ index に対して同じ結果を返す。
    - index: そのプロジェクト内でのカットSEイベント番号（0始まり）。
    - manifest: load_sfx_manifest() が返す正規化済み manifest リスト
                （各要素は少なくとも {"file","family","tags"} を持つ dict、
                 任意で "source" を持つ）。
    - family_weights: {"family_name": 重み(int)} の辞書。
                      Noneなら DEFAULT_FAMILY_WEIGHTS を使う。
                      重みが正のファミリーだけが候補になる。
    - avoid_file: 直前に採用した file 名（同一動画内の連続同音を避けるため）。
                  同じ file が選ばれた場合は、決定論的にリストの次のエントリへずらす。

    「音色のDNA」問題対策として、同一family内で source=="external"(外部実物SFX)
    が候補にあれば source=="gen"/None(合成SFX)より優先する（4:1 の重み比）。
    長尺タグ(LONG_TAG)のエントリはカットSE候補から除外する
    （長尺は環境音/riser など別用途向けで、短いカットSEには向かないため）。

    Returns: 選ばれたエントリ dict、候補が1つも無ければ None。
    """
    if not manifest:
        return None

    # カットSE向けにフィルタ（長尺は除外）
    cut_manifest = _filter_cut_sfx_candidates(manifest)
    if not cut_manifest:
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
        return [e for e in cut_manifest if e.get("family") == fam]

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
        candidates = _entries_for(fam) if fam is not None else list(cut_manifest)
        if not candidates:
            continue
        picked = _weighted_pick_from_candidates(candidates, seed_int, index, avoid_file)
        if picked is not None:
            return picked
        # このファミリー内は avoid_file と全衝突 → 次ファミリーへフォールバック
        # （最後の保険用に先頭だけ覚えておく）
        if last_resort is None:
            last_resort = candidates[0]

    # どのファミリーでも avoid_file を回避できなかった場合の最後の保険
    return last_resort


def _load_edit_recipes_raw():
    """assets/profiles/edit_recipes.json を読んで dict を返す。読めない/形式不正なら None。"""
    try:
        data = json.loads(_EDIT_RECIPES_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        return None
    return None


def load_edit_recipes():
    """assets/profiles/edit_recipes.json を返す（正規化不要のまま）。無ければ None。"""
    return _load_edit_recipes_raw()


def _mood_weight_map(recipes_data, bgm_mood):
    """bgm_mood → {recipe_name: weight} の辞書を解決する。

    edit_recipes.json の "mood_weights" セクションを引く。未知mood or セクション欠落
    のときは "default" キーへフォールバックし、それも無ければ None（＝レシピ選択なし）
    を返す（呼び出し側は ttp_reference の既定 edit セクションへフォールバックする）。
    """
    if not isinstance(recipes_data, dict):
        return None
    mood_weights = recipes_data.get("mood_weights") or {}
    if not isinstance(mood_weights, dict):
        return None
    if bgm_mood and bgm_mood in mood_weights and isinstance(mood_weights[bgm_mood], dict):
        return mood_weights[bgm_mood]
    default = mood_weights.get("default")
    if isinstance(default, dict):
        return default
    return None


def pick_edit_recipe(project_seed, bgm_mood, recipes_data=None):
    """project_seed + bgm_mood から使う編集レシピ名を決定論的に選ぶ。

    - recipes_data: load_edit_recipes() の戻り値。None を渡すとその場で読み込む。
    - 戻り値: (recipe_name:str, recipe_body:dict) / 選択できなければ (None, None)。

    「同じ台本でも別プロジェクト（別seed）なら別レシピ」「同じmoodでも別seedなら
    別レシピの傾向」を実現するため、seed_int + mood_key を組み合わせて mood_weights の
    重み分布から1つ選ぶ。project_seed が None のときは選ばない（後方互換：既存の
    ttp_reference edit セクションを使う）。
    """
    if project_seed is None:
        return None, None
    if recipes_data is None:
        recipes_data = _load_edit_recipes_raw()
    if not isinstance(recipes_data, dict):
        return None, None
    recipes = recipes_data.get("recipes") or {}
    if not isinstance(recipes, dict) or not recipes:
        return None, None
    weights = _mood_weight_map(recipes_data, bgm_mood)
    if not weights:
        return None, None
    # weight>0 かつ recipes に実在するもののみ候補
    candidates = sorted(
        [(name, float(w)) for name, w in weights.items() if isinstance(w, (int, float)) and w > 0 and name in recipes],
        key=lambda kv: (-kv[1], kv[0]),
    )
    if not candidates:
        return None, None
    seed_int = _seed_int("{}::{}".format(project_seed, bgm_mood or ""))
    total = sum(w for _, w in candidates)
    r = _rand01(seed_int, 0) * total
    cumulative = 0.0
    picked = candidates[-1][0]
    for name, w in candidates:
        cumulative += w
        if r < cumulative:
            picked = name
            break
    body = recipes.get(picked) or {}
    if not isinstance(body, dict):
        return None, None
    return picked, body


def _deep_merge_recipe(base, recipe_body):
    """ttp_reference 由来の base プロファイル(dict)に、レシピ本体を1段だけ深くマージする。

    各セクション（cut_sfx / punch_in / bgm_curve / first_shot_impact）はレシピ側の
    キーで上書き（他キーは base 側を維持）。レシピに無いセクションは base のまま。
    """
    if not isinstance(recipe_body, dict):
        return base
    for section, override in recipe_body.items():
        if not isinstance(override, dict):
            continue
        base_section = base.get(section)
        if not isinstance(base_section, dict):
            base[section] = dict(override)
            continue
        merged_section = dict(base_section)
        merged_section.update(override)
        base[section] = merged_section
    return base


def load_edit_profile(cfg=None, project_seed=None, bgm_mood=None):
    """ttp_reference.json の "edit" セクションを読み、既定値で補完して返す。

    cfg引数は他のローダー（load_config等）とインターフェースを揃えるために受け取るが、
    現状は未使用（将来プロジェクトごとの上書きに拡張する余地を残すため）。
    戻り値の各section dictには "resolved_path"（cut_sfxのみ・実パスまたはNone）を追加する。

    project_seed + bgm_mood が渡された場合、assets/profiles/edit_recipes.json の
    レシピを決定論的に選び、その内容をベース(ttp_reference 既定)にマージして返す。
    レシピが選ばれた場合は戻り値に "edit_recipe" キー（レシピ名）を含める。
    ttp_reference.json のeditセクションは既定フォールバックとして残る（後方互換）。
    """
    raw_edit = None
    try:
        data = json.loads(_TTP_REFERENCE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            raw_edit = data.get("edit")
    except Exception:
        raw_edit = None

    merged = _merge_edit_profile(raw_edit)

    # レシピ選択（backward-compat: project_seed が無ければ何もしない）。
    # レシピは ttp_reference 既定に対する差分としてマージするため、
    # レシピが指定しないキーは ttp_reference の既定値のまま残る。
    recipe_name, recipe_body = pick_edit_recipe(project_seed, bgm_mood)
    if recipe_name and isinstance(recipe_body, dict):
        merged = _deep_merge_recipe(merged, recipe_body)
        merged["edit_recipe"] = recipe_name

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
    # レシピが cut_sfx.file を明示している場合も固定モード扱い
    if isinstance(recipe_body, dict):
        recipe_cut = recipe_body.get("cut_sfx")
        if isinstance(recipe_cut, dict) and recipe_cut.get("file"):
            user_specified_file = True

    merged["cut_sfx"]["manifest"] = load_sfx_manifest()
    merged["cut_sfx"]["rotate"] = not user_specified_file
    return merged

# BUG_INVENTORY — R2a (F5 BPM/beat + F1 マルチ手法カット + fidelity 拡張)

R2a ラウンドで発見・修正した不具合の棚卸し。

## 第1周（自己レビュー + codex-review）

### BUG-R2a-01 [P1] 検出器 confidence が LLM 出力に上書きされる
- 場所: `pipeline/reference_v2.py:_normalize_v2_from_llm`
- 症状: fusion LLM が非空の `cuts` を返す通常経路で、`cut_confidence_map`
  が捨てられ LLM 推定の confidence が採用されてしまう（決定論的な検出器
  由来のアンサンブル confidence を活かせない）。
- 修正: 検出器由来の cuts があれば **常に** `_build_cuts_field(cuts,
  cut_confidence_map)` で上書き。LLM 出力の cuts は無視する（LLM が秒/
  confidence を再構築する精度は fusion prompt からのコピペ依存で不安定）。
- 検証: pytest 全緑（1045）＋ E2E で ensemble 結果が spec に届くこと確認。

### BUG-R2a-02 [P2] 不作動検出器が confidence 分母を膨らませる
- 場所: `pipeline/reference_v2.py:detect_cuts_via_pyscenedetect`,
  `merge_cut_lists`, `detect_cuts_ensemble`
- 症状: PySceneDetect が未インストール or `open_video` で失敗して空リスト
  を返しても `merge_cut_lists` は「動いて 0 件」と区別できず、分母に含めて
  しまう。全 ffmpeg 検出器が unanimous でも合議 confidence が 0.75 に留まり
  high 判定に届かない。
- 修正: `detect_cuts_via_pyscenedetect` の返り値を「不作動=None / 動いて
  0件=[]」で明確に区別。`merge_cut_lists` は None を分母から除外して
  active detectors だけで合議する。`detect_cuts_ensemble` も ffmpeg 例外時に
  None を積む。
- 検証: E2E synthetic (4 検出器・全カット unanimous) の confidence が
  0.75 → 1.00 に改善したことを実測。

## 第2周（自己検証）
1. 全テスト再実行: `pytest -q` → 1045 passed（1023 baseline + 22 新規）
2. E2E synthetic 再実行: `/tmp/e2e_r2a_check.py`
   - single scdet@0.30 は 3/4 のカットしか検出できず（6.0s の緑→青遷移を取り
     こぼす）、ensemble は 4/4 検出。unanimous confidence=1.0 を維持。
   - BPM=117.45（真値 120、誤差 -2.5）／beats_count=27／confidence=1.0。
   - beat_snap ON で境界が拍上に吸着（3.019s, 3.019s = 5〜6拍分）。
   - fidelity summary に beat_alignment が加わり、OFF/ON 両方で 1.0（合成
     カット位置自体が既に BPM に近いため）。
3. 実 TikTok での E2E: yt-dlp が TikTok 抽出に失敗（binding/venv 双方）。
   ネット・yt-dlp 側の課題で解析入口に到達不能 → 実測ではなく synthetic
   での機能検証のみ完了。次回 yt-dlp バイナリ更新後に再実測。

## 申し送り（R2b）
- テロップ専用高解像度 OCR（F8）／ナレーションプロソディ抽出（F7）／SFX
  音色マッチング（F6）は今ラウンドで手つかず。
- R2a fidelity では音楽が無い spec（旧キャッシュ）で beat_alignment=None
  を返すため、旧仕様の比較レポートでは「未測定」と区別すること。

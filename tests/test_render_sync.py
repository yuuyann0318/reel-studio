# -*- coding: utf-8 -*-
"""音声主導タイミング同期モード用の pipeline/render.py 追加関数の単体テスト。

純関数（コマンド構築・尺計算）は既存tests/test_render.pyと同じ思想でffmpeg実行なしに検証する。
末尾の1本のみ実ffmpeg（bin/ffmpeg・bin/ffprobe、無音断片×2→concat→ffprobe）で実行結果を検証する。
"""
from pipeline import render
from pipeline.config import project_root


# --- compute_synced_shot_duration -------------------------------------------------


def test_compute_synced_shot_duration_adds_gap():
    assert render.compute_synced_shot_duration(2.0) == 2.0 + render.SYNC_GAP_SEC


def test_compute_synced_shot_duration_floors_at_minimum():
    assert render.compute_synced_shot_duration(0.1) == render.SYNC_MIN_SHOT_SEC


# --- resolve_normalize_pad_args ---------------------------------------------------


def test_resolve_normalize_pad_args_no_target_passes_through_unchanged():
    duration_sec, pad = render.resolve_normalize_pad_args(3.0, None)
    assert duration_sec == 3.0
    assert pad is None


def test_resolve_normalize_pad_args_target_longer_than_content_pads():
    duration_sec, pad = render.resolve_normalize_pad_args(2.0, 3.25)
    assert duration_sec == 2.0
    assert pad == 3.25


def test_resolve_normalize_pad_args_target_shorter_than_content_truncates():
    duration_sec, pad = render.resolve_normalize_pad_args(5.0, 1.25)
    assert duration_sec == 1.25
    assert pad is None


def test_resolve_normalize_pad_args_target_equal_to_content_truncates_no_pad():
    duration_sec, pad = render.resolve_normalize_pad_args(2.0, 2.0)
    assert duration_sec == 2.0
    assert pad is None


# --- build_normalize_clip_cmd の pad_to_duration_sec 拡張 --------------------------


def test_build_normalize_clip_cmd_without_pad_is_unchanged():
    """pad_to_duration_sec未指定時は既存挙動と完全に同一（後方互換）。"""
    cmd_old = render.build_normalize_clip_cmd("/bin/ffmpeg", "/in.mp4", "/out.mp4", duration_sec=5.0)
    cmd_new = render.build_normalize_clip_cmd(
        "/bin/ffmpeg", "/in.mp4", "/out.mp4", duration_sec=5.0, pad_to_duration_sec=None
    )
    assert cmd_old == cmd_new
    assert "tpad" not in " ".join(cmd_old)


def test_build_normalize_clip_cmd_with_pad_adds_tpad_and_extends_t():
    cmd = render.build_normalize_clip_cmd(
        "/bin/ffmpeg", "/in.mp4", "/out.mp4", duration_sec=2.0, pad_to_duration_sec=3.25
    )
    joined = " ".join(cmd)
    assert "tpad=stop_mode=clone:stop_duration=1.250" in joined
    t_idx = cmd.index("-t")
    assert cmd[t_idx + 1] == "3.250"


def test_build_normalize_clip_cmd_pad_not_greater_than_duration_is_ignored():
    """pad_to_duration_sec <= duration_sec なら単純truncateのまま（tpadを付けない）。"""
    cmd = render.build_normalize_clip_cmd(
        "/bin/ffmpeg", "/in.mp4", "/out.mp4", duration_sec=5.0, pad_to_duration_sec=3.0
    )
    joined = " ".join(cmd)
    assert "tpad" not in joined
    t_idx = cmd.index("-t")
    assert cmd[t_idx + 1] == "5.000"


# --- build_narration_segments_placement_filters / build_narration_segments_concat_cmd ---


def test_build_narration_segments_placement_filters_produces_adelay_and_labels():
    segments = [{"path": "/a.wav", "start_sec": 0.0}, {"path": "/b.wav", "start_sec": 2.5}]
    parts, labels = render.build_narration_segments_placement_filters(segments, start_index=0)
    assert len(parts) == 2 and len(labels) == 2
    assert "[0:a]" in parts[0] and "adelay=0|0" in parts[0]
    assert "[1:a]" in parts[1] and "adelay=2500|2500" in parts[1]
    assert labels == ["[seg0]", "[seg1]"]


def test_build_narration_segments_concat_cmd_has_inputs_filter_and_output_duration():
    segments = [{"path": "/seg0.wav", "start_sec": 0.0}, {"path": "/seg1.wav", "start_sec": 1.5}]
    cmd = render.build_narration_segments_concat_cmd("/bin/ffmpeg", segments, 3.0, "/out.wav")
    assert cmd[0] == "/bin/ffmpeg"
    assert "/seg0.wav" in cmd and "/seg1.wav" in cmd
    joined = " ".join(cmd)
    assert "adelay=0|0" in joined
    assert "adelay=1500|1500" in joined
    assert "amix=inputs=2:duration=longest:normalize=0[out]" in joined
    assert "-map" in cmd and "[out]" in cmd
    t_idx = cmd.index("-t")
    assert cmd[t_idx + 1] == "3.000"
    assert cmd[-1] == "/out.wav"


def test_build_narration_segments_concat_cmd_empty_segments_falls_back_to_silence():
    cmd = render.build_narration_segments_concat_cmd("/bin/ffmpeg", [], 2.0, "/out.wav")
    joined = " ".join(cmd)
    assert "anullsrc" in joined
    assert cmd[-1] == "/out.wav"


# --- 実ffmpeg実行テスト: 無音断片2個 -> 配置concat -> ffprobeで境界(尺)検証 -----------


def test_narration_segments_concat_real_ffmpeg_places_segments_at_expected_offsets(tmp_path):
    """無音断片2個をずらして配置し、-t を指定せず自然尺(=最後方の断片のstart+duration)が
    ffprobeの実測でも一致することを確認する（adelay/amixの配置ロジックが正しいことの実証）。
    """
    ffmpeg_bin = str(project_root() / "bin" / "ffmpeg")
    ffprobe_bin = str(project_root() / "bin" / "ffprobe")

    seg0_path = tmp_path / "seg0.wav"
    seg1_path = tmp_path / "seg1.wav"
    # tts.SilentTTSBackendは最短2秒にクランプされる(_MIN_FALLBACK_SEC)ため、
    # 境界位置検証に必要な短尺・可変長の無音は直接ffmpegのanullsrcで生成する。
    import subprocess

    def _make_silence(path, duration_sec):
        subprocess.run(
            [
                ffmpeg_bin, "-y", "-f", "lavfi", "-i", "anullsrc=channel_layout=mono:sample_rate=48000",
                "-t", "{:.3f}".format(duration_sec), "-c:a", "pcm_s16le", str(path),
            ],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        )

    _make_silence(seg0_path, 0.6)
    _make_silence(seg1_path, 0.8)

    start1 = 1.5
    segments = [
        {"path": str(seg0_path), "start_sec": 0.0},
        {"path": str(seg1_path), "start_sec": start1},
    ]
    out_path = tmp_path / "narration.wav"
    # out_duration=None（自然尺のまま。-tで打ち切らない）にすることで、
    # adelay配置が壊れていれば実測尺がずれる=境界位置の検証になる。
    cmd = render.build_narration_segments_concat_cmd(ffmpeg_bin, segments, None, str(out_path))
    res = render.run_ffmpeg(cmd, timeout_sec=60)
    assert res["returncode"] == 0, res["stderr"]
    assert out_path.exists()

    expected_duration = start1 + 0.8  # 2番目の断片が最も後方 = 自然尺を決める
    proc = subprocess.run(
        [ffprobe_bin, "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(out_path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    measured = float(proc.stdout.decode("utf-8", "replace").strip())
    assert abs(measured - expected_duration) < 0.3

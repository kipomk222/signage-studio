from pathlib import Path

import pytest

from app import platform as pf
from app.platform import OSKind, SetupError


@pytest.mark.parametrize("name, kind", [
    ("Darwin", OSKind.MAC), ("Windows", OSKind.WINDOWS), ("Linux", OSKind.LINUX),
])
def test_detect_os(name, kind):
    assert pf.detect_os(name) is kind


def test_missing_ffmpeg_explains_what_to_do():
    with pytest.raises(SetupError) as e:
        pf.find_ffmpeg(OSKind.WINDOWS, which=lambda _name: None)
    assert "FFmpeg" in e.value.what
    assert "winget install ffmpeg" in e.value.how


def test_parse_encoder_list():
    text = """Encoders:
 V..... = Video
 ------
 V....D libx264              libx264 H.264 / AVC
 V....D h264_amf             AMD AMF H.264 Encoder
 A....D aac                  AAC (Advanced Audio Coding)
"""
    assert pf.parse_encoder_list(text) == {"libx264", "h264_amf"}


def test_windows_falls_back_when_amf_does_not_work():
    picked = pf.pick_encoder(OSKind.WINDOWS, {"h264_amf", "libx264"}, works=lambda n: n != "h264_amf")
    assert picked == "libx264"


def test_windows_uses_amf_when_it_works():
    assert pf.pick_encoder(OSKind.WINDOWS, {"h264_amf", "libx264"}, works=lambda n: True) == "h264_amf"


def test_mac_prefers_videotoolbox():
    assert pf.pick_encoder(OSKind.MAC, {"h264_videotoolbox", "libx264"}, works=lambda n: True) == "h264_videotoolbox"


def test_no_encoder_is_a_setup_error():
    with pytest.raises(SetupError):
        pf.pick_encoder(OSKind.MAC, set(), works=lambda n: True)


def test_open_folder_commands():
    p = Path("/tmp/x")
    assert pf.open_folder_command(p, OSKind.MAC)[0] == "open"
    assert pf.open_folder_command(p, OSKind.WINDOWS)[0] == "explorer"


@pytest.mark.parametrize("kind, backend", [
    (OSKind.MAC, "mlx-whisper"), (OSKind.WINDOWS, "faster-whisper"),
])
def test_build_profile_per_os(kind, backend):
    profile = pf.build_profile(
        kind,
        which=lambda name: f"/bin/{name}",
        encoders=lambda _ff: {"h264_videotoolbox", "h264_amf", "libx264"},
        works=lambda _ff, _n: True,
    )
    assert profile.whisper_backend == backend
    assert profile.video_encoder == pf.ENCODER_CANDIDATES[kind][0]


def test_real_profile_on_this_machine():
    """実際の FFmpeg で判別と書き出し方式の確認が通ること。"""
    pf.get_profile.cache_clear()
    profile = pf.get_profile()
    assert profile.video_encoder in pf.ENCODER_CANDIDATES[profile.os_kind]


def test_only_platform_module_branches_on_os():
    """OS 判定は platform.py にだけ書く（CLAUDE.md の作業ルール）。"""
    app_dir = Path(__file__).resolve().parents[1] / "app"
    offenders = [
        p.name for p in app_dir.rglob("*.py")
        if p.name != "platform.py"
        and any(word in p.read_text(encoding="utf-8") for word in ("sys.platform", "platform.system", "os.name =="))
    ]
    assert offenders == []


def test_six_fonts_are_bundled_with_licenses():
    fonts = Path(__file__).resolve().parents[1] / "assets" / "fonts"
    files = sorted(p.name for p in fonts.iterdir() if p.suffix in {".ttf", ".otf"})
    assert len(files) == 6
    assert len(list((fonts / "licenses").glob("OFL_*.txt"))) == 5  # Noto は Bold/Black で1つ

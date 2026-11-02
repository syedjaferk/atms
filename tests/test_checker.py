from PIL import Image

from atms_edge.checker import Clip, Verdict, check, discover, summary, to_mp4
from atms_edge.detector import AccidentDetector


def touch(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (64, 36)).save(path)


def test_frames_group_into_clips_with_labels(tmp_path):
    for n in (1, 2, 3):
        touch(tmp_path / "Accident" / f"test7_{n}.jpg")
    touch(tmp_path / "Accident" / "acc1 (2).jpg")
    touch(tmp_path / "Accident" / "acc1 (10).jpg")
    touch(tmp_path / "Non Accident" / "5_16.jpg")
    clips = {c.name.split("/", 1)[1]: c for c in discover(tmp_path)}
    assert [p.name for p in clips["Accident/acc1"].frames] == ["acc1 (2).jpg", "acc1 (10).jpg"]   # numeric order
    assert len(clips["Accident/test7"].frames) == 3
    assert clips["Accident/test7"].expected is True and clips["Non Accident/5"].expected is False


def fake(scores):
    it = iter(scores)
    box = {"xmin": 10, "ymin": 10, "xmax": 50, "ymax": 30}
    return AccidentDetector(lambda f: [{"label": "accident", "score": next(it), "box": box}], lambda f: [])


def test_single_frame_clip_can_still_be_confirmed(tmp_path):
    touch(tmp_path / "Accident" / "t_1.jpg")
    clip = discover(tmp_path)[0]
    v = check(fake([0.9]), clip, 5)
    assert v.accident and v.scores == [0.9]


def test_summary_counts_agreement():
    yes, no = Clip("a", expected=True), Clip("b", expected=False)
    d = fake([0.9]).scan([Image.new("RGB", (64, 36))], 1)[0]
    s = summary([Verdict(yes, d, [0.9]), Verdict(yes, None, [0.0]), Verdict(no, None, [0.0]), Verdict(no, d, [0.9])])
    assert (s["true_positive"], s["false_negative"], s["true_negative"], s["false_positive"]) == (1, 1, 1, 1)
    assert s["accuracy"] == 0.5


def test_frames_stitch_to_an_h264_clip(tmp_path):
    import shutil
    import subprocess

    import pytest

    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    for n in (1, 2):
        touch(tmp_path / "f" / f"c_{n}.jpg")
    out = to_mp4(discover(tmp_path)[0], tmp_path / "o.mp4")
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(out)],
                           capture_output=True, text=True)
    assert probe.stdout.strip() == "h264"

import json, pathlib, subprocess, tempfile

def run(args):
    subprocess.run(args, check=True, timeout=120, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

with tempfile.TemporaryDirectory(prefix="prompt-studio-self-test-") as folder:
    root = pathlib.Path(folder)
    clip_a = root / "test_a.mp4"
    clip_b = root / "test_b.mp4"
    listing = root / "concat.txt"
    output = root / "self-test-output.mp4"
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=30", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(clip_a)])
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:size=640x360:rate=30", "-f", "lavfi", "-i", "sine=frequency=660:sample_rate=48000", "-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(clip_b)])
    listing.write_text("file '%s'\nfile '%s'\n" % (clip_a.as_posix(), clip_b.as_posix()), encoding="utf-8")
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", "-movflags", "+faststart", str(output)])
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-show_entries", "stream=codec_type,codec_name", "-of", "json", str(output)], check=True, capture_output=True, text=True, timeout=30)
    result = json.loads(probe.stdout)
    duration = float(result["format"]["duration"])
    streams = result.get("streams", [])
    codecs = {item.get("codec_type") for item in streams}
    if not (3.8 <= duration <= 4.2) or not {"video", "audio"}.issubset(codecs):
        raise RuntimeError("FFmpeg self-test output failed duration or audio/video validation.")
    print("PASS: GitHub runner, FFmpeg H.264/AAC encoding, clip concatenation, and ffprobe validation.")
    print("Synthetic output duration: %.2f seconds. No user media or secrets were used." % duration)

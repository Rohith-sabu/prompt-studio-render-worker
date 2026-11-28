import json, os, pathlib, subprocess, tempfile, urllib.request, urllib.parse
from urllib.parse import urlparse

API = os.environ["API_BASE"].rstrip("/")
JOB = os.environ["JOB_ID"]
AUD = "prompt-studio-render-worker"
parsed_api = urlparse(API)
host = (parsed_api.hostname or "").lower()
if parsed_api.scheme != "https" or not (host == "floot.com" or host.endswith(".floot.com") or host.endswith(".floot.app")):
    raise RuntimeError("Refusing an untrusted Prompt Studio API origin.")

def oidc():
    url = os.environ["ACTIONS_ID_TOKEN_REQUEST_URL"] + "&audience=" + urllib.parse.quote(AUD)
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + os.environ["ACTIONS_ID_TOKEN_REQUEST_TOKEN"]})
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.loads(response.read())["value"]

TOKEN = oidc()

def post(path, data):
    req = urllib.request.Request(
        API + path,
        data=json.dumps(data).encode(),
        headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=90) as response:
        return json.loads(response.read())

def progress(value, message):
    try:
        post("/_api/render-worker/progress", {"jobId": JOB, "progress": value, "message": message[:180]})
    except Exception as error:
        print("Progress update skipped:", str(error)[:180])

def download(url, path):
    req = urllib.request.Request(url, headers={"User-Agent": "PromptStudioRenderer/1.0"})
    with urllib.request.urlopen(req, timeout=180) as response, open(path, "wb") as output:
        total = 0
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > 110 * 1024 * 1024:
                raise RuntimeError("A source download exceeded the 110 MB safety limit.")
            output.write(chunk)

def has_audio(path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=index", "-of", "csv=p=0", str(path)],
        check=True, capture_output=True, text=True, timeout=30
    )
    return bool(result.stdout.strip())

try:
    with tempfile.TemporaryDirectory(prefix="prompt-studio-") as tmp:
        root = pathlib.Path(tmp)
        manifest = post("/_api/render-worker/manifest", {"jobId": JOB})
        if not manifest.get("ok"):
            raise RuntimeError(manifest.get("error", "Manifest unavailable"))
        sources = manifest.get("sources") or []
        if not sources:
            raise RuntimeError("No video clips were provided.")
        if len(sources) > 12:
            raise RuntimeError("This renderer accepts at most 12 video clips per job.")

        normalized = []
        for index, source in enumerate(sources):
            source_path = root / ("source_%03d.bin" % index)
            download(source["url"], source_path)
            normalized_path = root / ("normalized_%03d.mp4" % index)
            video_filter = "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,fps=30,setsar=1"
            command = ["ffmpeg", "-hide_banner", "-y", "-i", str(source_path)]
            if has_audio(source_path):
                command += ["-map", "0:v:0", "-map", "0:a:0"]
            else:
                command += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000", "-map", "0:v:0", "-map", "1:a:0"]
            command += ["-vf", video_filter, "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2", "-shortest", "-movflags", "+faststart", str(normalized_path)]
            subprocess.run(command, check=True, timeout=5 * 60 * 60)
            normalized.append(normalized_path)
            progress(15 + int((index + 1) / len(sources) * 45), "Normalized source clip %d of %d" % (index + 1, len(sources)))

        listing = root / "concat.txt"
        entries = ["file '%s'" % path.as_posix().replace("'", "'\\''") for path in normalized]
        listing.write_text("\n".join(entries) + "\n", encoding="utf-8")
        output = root / "render.mp4"
        command = ["ffmpeg", "-hide_banner", "-y", "-f", "concat", "-safe", "0", "-i", str(listing)]
        if manifest.get("audio"):
            soundtrack = root / "soundtrack.bin"
            download(manifest["audio"]["url"], soundtrack)
            command += ["-stream_loop", "-1", "-i", str(soundtrack), "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest"]
        else:
            command += ["-map", "0:v:0", "-map", "0:a:0", "-c", "copy"]
        command += ["-movflags", "+faststart", str(output)]
        progress(65, "Assembling normalized clips with FFmpeg")
        subprocess.run(command, check=True, timeout=5 * 60 * 60)

        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(output)],
            check=True, capture_output=True, text=True, timeout=30
        )
        duration = float(probe.stdout.strip())
        size = output.stat().st_size
        if duration <= 0 or size <= 0 or size > 500 * 1024 * 1024:
            raise RuntimeError("Output is empty, has invalid duration, or exceeds 500 MB.")

        progress(82, "Uploading the completed video to private project storage")
        ticket = post("/_api/render-worker/upload", {"jobId": JOB, "sizeBytes": size})
        if not ticket.get("ok"):
            raise RuntimeError(ticket.get("error", "Private upload ticket failed."))
        request = urllib.request.Request(
            ticket["presignedUrl"],
            data=output.read_bytes(),
            headers=ticket.get("headers") or {"Content-Type": "video/mp4"},
            method="PUT",
        )
        with urllib.request.urlopen(request, timeout=600) as response:
            if response.status not in (200, 201, 204):
                raise RuntimeError("Private storage upload failed.")

        result = post("/_api/render-worker/complete", {
            "jobId": JOB,
            "filename": ticket["filename"],
            "sizeBytes": size,
        })
        if not result.get("ok"):
            raise RuntimeError(result.get("error", "Could not finalize output."))
        print("Render saved privately to Prompt Studio. Asset ID:", result["assetId"])
except Exception as error:
    try:
        post("/_api/render-worker/fail", {"jobId": JOB, "error": str(error)[:1000]})
    except Exception:
        pass
    raise

import json, os, pathlib, subprocess, tempfile, urllib.request, urllib.parse

API = os.environ["API_BASE"].rstrip("/")
JOB = os.environ["JOB_ID"]
AUD = "prompt-studio-render-worker"

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

def download(url, path):
    req = urllib.request.Request(url, headers={"User-Agent": "PromptStudioRenderer/1.0"})
    with urllib.request.urlopen(req, timeout=180) as response, open(path, "wb") as output:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            output.write(chunk)

try:
    with tempfile.TemporaryDirectory(prefix="prompt-studio-") as tmp:
        root = pathlib.Path(tmp)
        manifest = post("/_api/render-worker/manifest", {"jobId": JOB})
        if not manifest.get("ok"):
            raise RuntimeError(manifest.get("error", "Manifest unavailable"))

        clips = []
        for index, source in enumerate(manifest["sources"]):
            path = root / ("clip_%03d.bin" % index)
            download(source["url"], path)
            clips.append(path)
        if not clips:
            raise RuntimeError("No video clips were provided.")

        listing = root / "concat.txt"
        entries = ["file '%s'" % path.as_posix().replace("'", "'\\''") for path in clips]
        listing.write_text("\n".join(entries) + "\n", encoding="utf-8")

        output = root / "render.mp4"
        command = ["ffmpeg", "-hide_banner", "-y", "-f", "concat", "-safe", "0", "-i", str(listing)]
        if manifest.get("audio"):
            soundtrack = root / "soundtrack.bin"
            download(manifest["audio"]["url"], soundtrack)
            command += ["-i", str(soundtrack), "-map", "0:v:0", "-map", "1:a:0", "-shortest", "-c:a", "aac", "-b:a", "192k"]
        else:
            command += ["-map", "0:v:0", "-map", "0:a?"]
        command += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(output)]
        subprocess.run(command, check=True, timeout=5 * 60 * 60)

        size = output.stat().st_size
        if size <= 0 or size > 500 * 1024 * 1024:
            raise RuntimeError("Output is empty or exceeds 500 MB.")

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

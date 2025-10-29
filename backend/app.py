from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import sys

from flask import Flask, abort, after_this_request, jsonify, request, send_file

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.append(str(ROOT_DIR))

from downloader import DownloadError, download_video

app = Flask(__name__)


@app.get("/")
def root() -> tuple[dict[str, str], int]:
    return {"status": "ok", "message": "YT Downloader backend running"}, 200


@app.post("/download")
def download_endpoint():
    payload = request.get_json(silent=True) or {}

    url = payload.get("url")
    if not url:
        abort(400, description="Missing 'url' in request body")

    audio_only = bool(payload.get("audio_only", False))
    resolution = payload.get("resolution")
    filename = payload.get("filename")
    cookies = payload.get("cookies")

    temp_dir = TemporaryDirectory(prefix="yt-download-")
    output_dir = Path(temp_dir.name)

    try:
        destination = download_video(
            url=url,
            output_dir=output_dir,
            filename=filename,
            audio_only=audio_only,
            resolution=resolution,
            progress_callback=None,
            control=None,
            cookies=cookies,
        )
    except DownloadError as exc:
        temp_dir.cleanup()
        abort(400, description=str(exc))
    except Exception as exc:  # pragma: no cover - defensive
        temp_dir.cleanup()
        abort(500, description="Failed to download video")

    file_path = Path(destination)
    if not file_path.exists():
        temp_dir.cleanup()
        abort(500, description="Download destination not found")

    @after_this_request
    def _cleanup(response):
        try:
            if file_path.exists():
                file_path.unlink()
        except Exception:
            pass
        try:
            temp_dir.cleanup()
        except Exception:
            pass
        return response

    return send_file(file_path, as_attachment=True, download_name=file_path.name)

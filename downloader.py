#!/usr/bin/env python3
"""Simple CLI YouTube video downloader using pytube with yt-dlp fallback."""

from __future__ import annotations

import argparse
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Protocol
import shutil

from pytube import YouTube
from yt_dlp import YoutubeDL


ProgressCallback = Callable[[float | None, str], None]


class DownloadError(RuntimeError):
    """Raised when all download strategies fail."""


class DownloadCancelled(RuntimeError):
    """Raised when a download is cancelled by the caller."""


class DownloadControl(Protocol):
    """Protocol describing cooperative cancellation and pausing controls."""

    def wait_if_paused(self) -> None:
        """Block while the caller requests a pause."""

    def raise_if_cancelled(self) -> None:
        """Raise an exception if cancellation has been requested."""


def _safe_write(stream: Any, message: str) -> None:
    """Write to a stream if available, falling back gracefully when detached."""

    target = stream if stream and hasattr(stream, "write") else None

    if target is None:
        if stream is sys.stderr:
            target = getattr(sys, "__stderr__", None)
        else:
            target = getattr(sys, "__stdout__", None)

    if target and hasattr(target, "write"):
        try:
            target.write(message)
            if hasattr(target, "flush"):
                target.flush()
        except Exception:  # pragma: no cover - defensive fallback
            pass


def _human_readable_size(num_bytes: int) -> str:
    """Convert bytes to a more readable string."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num_bytes < 1024 or unit == "TB":
            return f"{num_bytes:.2f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.2f} TB"


def _progress_callback(stream, chunk, bytes_remaining):  # type: ignore[no-untyped-def]
    total_size = stream.filesize
    bytes_downloaded = total_size - bytes_remaining
    percent = bytes_downloaded / total_size * 100
    _safe_write(
        sys.stdout,
        f"\rDownloading: {percent:5.1f}% ({_human_readable_size(bytes_downloaded)} / {_human_readable_size(total_size)})",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Download YouTube videos using pytube.")
    parser.add_argument("url", help="The full YouTube video URL to download.")
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=Path.cwd(),
        help="Directory where the file will be saved (defaults to current working directory).",
    )
    parser.add_argument(
        "-n",
        "--filename",
        help="Optional custom filename without extension (defaults to video title).",
    )
    parser.add_argument(
        "-a",
        "--audio-only",
        action="store_true",
        help="Download only the audio stream (highest bitrate).",
    )
    parser.add_argument(
        "-q",
        "--resolution",
        help="Preferred video resolution (e.g., 1080p). Highest available if omitted.",
    )
    return parser


def download_video(
    url: str,
    output_dir: Path,
    filename: str | None,
    audio_only: bool,
    resolution: str | None,
    progress_callback: ProgressCallback | None = None,
    control: DownloadControl | None = None,
    cookies: str | None = None,
) -> Path:
    prefer_ytdlp = audio_only or bool(cookies)

    if prefer_ytdlp:
        try:
            return _download_with_ytdlp(
                url,
                output_dir,
                filename,
                audio_only,
                resolution,
                progress_callback,
                control,
                cookies,
            )
        except DownloadCancelled:
            raise
        except Exception as exc:  # pylint: disable=broad-except
            if audio_only or cookies:
                raise DownloadError(str(exc)) from exc

    try:
        return _download_with_pytube(
            url,
            output_dir,
            filename,
            audio_only,
            resolution,
            progress_callback,
            control,
        )
    except DownloadCancelled:
        raise
    except Exception as exc:  # pylint: disable=broad-except
        _safe_write(sys.stderr, f"pytube failed ({exc!s}); falling back to yt-dlp...\n")
        try:
            return _download_with_ytdlp(
                url,
                output_dir,
                filename,
                audio_only,
                resolution,
                progress_callback,
                control,
                cookies,
            )
        except DownloadCancelled:
            raise
        except Exception as fallback_exc:  # pylint: disable=broad-except
            raise DownloadError(str(fallback_exc)) from fallback_exc


def _download_with_pytube(
    url: str,
    output_dir: Path,
    filename: str | None,
    audio_only: bool,
    resolution: str | None,
    progress_callback: ProgressCallback | None,
    control: DownloadControl | None,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)

    if control:
        control.raise_if_cancelled()

    yt = YouTube(
        url,
        on_progress_callback=_make_pytube_progress_handler(progress_callback, control),
    )

    if audio_only:
        stream = yt.streams.filter(only_audio=True).order_by("abr").desc().first()
        if stream is None:
            raise ValueError("No audio streams available for this video.")
    else:
        filtered_streams = yt.streams.filter(progressive=True, file_extension="mp4")
        if resolution:
            stream = filtered_streams.filter(res=resolution).first()
            if stream is None:
                raise ValueError(f"Resolution {resolution} not available. Use --resolution to pick another.")
        else:
            stream = filtered_streams.order_by("resolution").desc().first()
        if stream is None:
            raise ValueError("No progressive MP4 streams available for this video.")

    if control:
        control.raise_if_cancelled()
        control.wait_if_paused()

    filename = filename or stream.default_filename
    destination = stream.download(output_path=str(output_dir), filename=filename)

    if progress_callback:
        progress_callback(100.0, "")
    else:
        _safe_write(sys.stdout, "\nDownload complete: {0}\n".format(destination))
    return Path(destination)


def _download_with_ytdlp(
    url: str,
    output_dir: Path,
    filename: str | None,
    audio_only: bool,
    resolution: str | None,
    progress_callback: ProgressCallback | None,
    control: DownloadControl | None,
    cookies: str | None,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)

    template_name, final_extension = _determine_template(filename, audio_only)

    if audio_only:
        format_selector = "bestaudio/best"
        merge_output_format = None
    else:
        height = _parse_resolution(resolution) if resolution else None
        height_filter = f"[height<={height}]" if height else ""
        progressive_fallback = f"best{height_filter}[ext=mp4][acodec!=none]"
        format_selector = (
            f"bestvideo{height_filter}[ext=mp4]+bestaudio[ext=m4a]/"
            f"bestvideo{height_filter}+bestaudio/"
            f"{progressive_fallback}/"
            "best"
        )
        merge_output_format = "mp4"

    download_info: dict[str, Path] = {}

    def _hook(status: dict[str, Any]) -> None:
        if control:
            control.raise_if_cancelled()
            control.wait_if_paused()

        status_type = status.get("status")
        if status_type == "downloading" and progress_callback:
            downloaded = status.get("downloaded_bytes") or 0
            total = status.get("total_bytes") or status.get("total_bytes_estimate") or 0
            percent = (downloaded / total * 100) if total else None
            progress_callback(percent, "")
        elif status_type == "postprocessing" and progress_callback:
            progress_callback(None, "")
        elif status_type == "finished":
            if status.get("filename"):
                download_info["filepath"] = Path(status["filename"]).resolve()
            if progress_callback:
                progress_callback(100.0, "")
            else:
                _safe_write(sys.stdout, "\nDownload complete: {0}\n".format(status.get("filename", "")))

    ydl_opts: dict[str, object] = {
        "outtmpl": template_name,
        "format": format_selector,
        "noprogress": False,
        "progress_hooks": [_hook],
        "quiet": False,
        "overwrites": True,
    }

    ffmpeg_path = Path(__file__).resolve().parent / "ffmpeg.exe"
    if ffmpeg_path.exists():
        ydl_opts["ffmpeg_location"] = str(ffmpeg_path)
    else:
        fallback_ffmpeg = shutil.which("ffmpeg")
        if fallback_ffmpeg:
            ydl_opts["ffmpeg_location"] = fallback_ffmpeg

    if merge_output_format:
        ydl_opts["merge_output_format"] = merge_output_format

    if audio_only:
        ydl_opts["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ]
        if final_extension:
            ydl_opts["postprocessor_args"] = ["-vn"]
            ydl_opts["outtmpl"] = template_name.replace("%(ext)s", final_extension)

    with tempfile.TemporaryDirectory(prefix="yt-dlp-frag-") as temp_dir:
        ydl_opts["paths"] = {
            "home": str(output_dir),
            "temp": temp_dir,
        }

        if cookies:
            cookies = cookies.strip()
            if "\n" in cookies or "\t" in cookies:
                cookiefile = Path(temp_dir) / "cookies.txt"
                cookiefile.write_text(cookies, encoding="utf-8")
                ydl_opts["cookiefile"] = str(cookiefile)
            else:
                headers = ydl_opts.setdefault("http_headers", {})
                headers["Cookie"] = cookies

        with YoutubeDL(ydl_opts) as ydl:
            if control:
                control.raise_if_cancelled()
            try:
                ydl.download([url])
            except DownloadCancelled:
                raise

    final_path = download_info.get("filepath")
    if not final_path:
        raise RuntimeError("yt-dlp completed without reporting a destination file.")
    if audio_only and final_extension and final_path.suffix != f".{final_extension}":
        target = final_path.with_suffix(f".{final_extension}")
        if final_path.exists():
            try:
                if target.exists():
                    target.unlink()
                final_path.rename(target)
                final_path = target
            except OSError:
                pass
    return final_path


def _determine_template(filename: str | None, audio_only: bool) -> tuple[str, str | None]:
    ext = "mp3" if audio_only else None
    if filename:
        name = Path(filename)
        if name.suffix:
            if audio_only:
                return str(name.with_suffix(".mp3")), "mp3"
            return str(name), name.suffix.lstrip(".")
        return (f"{name.name}.%(ext)s", ext)
    return ("%(title)s.%(ext)s", ext)


def _make_pytube_progress_handler(
    progress_callback: ProgressCallback | None,
    control: DownloadControl | None,
):  # type: ignore[override]
    if progress_callback is None:
        return _progress_callback

    def _handler(stream, chunk, bytes_remaining):  # type: ignore[no-untyped-def]
        if control:
            control.raise_if_cancelled()
            control.wait_if_paused()

        total_size = getattr(stream, "filesize", None) or getattr(stream, "filesize_approx", 0)
        downloaded = (total_size - bytes_remaining) if total_size else 0
        percent = (downloaded / total_size * 100) if total_size else None
        progress_callback(percent, "")

    return _handler


def _parse_resolution(resolution: str) -> int | None:
    match = re.match(r"^(\d+)", resolution.strip())
    if match:
        return int(match.group(1))
    return None


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        download_video(
            url=args.url,
            output_dir=args.output_dir,
            filename=args.filename,
            audio_only=args.audio_only,
            resolution=args.resolution,
        )
    except Exception as exc:  # pylint: disable=broad-except
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

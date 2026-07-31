from __future__ import annotations

import hashlib
import html
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

SOURCE_URL = "https://www.instagram.com/reel/DbYpNFeMNKH/"
WORK = Path("reel-build")
DOWNLOADS = WORK / "downloads"
SOURCE = WORK / "source.mp4"
VIDEO = WORK / "leuchtfeuer-hype-reel.mp4"
AUDIO = WORK / "leuchtfeuer-hype-reel.mp3"
POSTER = WORK / "leuchtfeuer-hype-reel-poster.webp"
FRAME = WORK / "validation-frame.jpg"
MANIFEST = WORK / "manifest.json"

TRUSTED_SUFFIXES = (
    "cliplatch.com",
    "cdninstagram.com",
    "fbcdn.net",
    "nativeig.vercel.app",
    "instagram-reels-downloader-tau.vercel.app",
    "instagram.com",
)


def run(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    print("+", " ".join(args), flush=True)
    proc = subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.stdout:
        print(proc.stdout[-4000:], flush=True)
    if proc.stderr:
        print(proc.stderr[-8000:], file=sys.stderr, flush=True)
    if check and proc.returncode != 0:
        raise RuntimeError(f"Command exited {proc.returncode}: {' '.join(args)}")
    return proc


def ffprobe(path: Path) -> dict[str, Any]:
    proc = run(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        check=False,
    )
    if proc.returncode != 0:
        return {"streams": [], "format": {}, "error": proc.stderr[-1200:]}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"streams": [], "format": {}, "error": "invalid ffprobe JSON"}


def streams_of_kind(probe: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [stream for stream in probe.get("streams", []) if stream.get("codec_type") == kind]


def has_video(probe: dict[str, Any]) -> bool:
    return bool(streams_of_kind(probe, "video"))


def has_audio(probe: dict[str, Any]) -> bool:
    return bool(streams_of_kind(probe, "audio"))


def trusted_url(value: str) -> str | None:
    value = html.unescape(value.strip())
    if not value:
        return None
    try:
        url = urljoin("https://cliplatch.com", value)
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.scheme != "https" or not parsed.hostname:
        return None
    if not any(parsed.hostname == suffix or parsed.hostname.endswith(f".{suffix}") for suffix in TRUSTED_SUFFIXES):
        return None
    return url


def collect_urls(value: Any, trail: str = "root", output: list[tuple[str, str]] | None = None) -> list[tuple[str, str]]:
    if output is None:
        output = []
    if isinstance(value, str):
        url = trusted_url(value)
        if url:
            output.append((url, trail))
    elif isinstance(value, list):
        for index, entry in enumerate(value):
            collect_urls(entry, f"{trail}[{index}]", output)
    elif isinstance(value, dict):
        for key, entry in value.items():
            collect_urls(entry, f"{trail}.{key}", output)
    return output


def unique(items: list[tuple[str, str]]) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in items:
        if item[0] in seen:
            continue
        seen.add(item[0])
        result.append(item)
    return result


def quality_hint(text: str) -> int:
    for value in (2160, 1440, 1080, 720, 540, 480, 360, 240):
        if str(value) in text:
            return value
    return 0


def video_score(item: tuple[str, str]) -> int:
    url, trail = item
    text = f"{trail} {url}".lower()
    score = quality_hint(text)
    if "/api/merge" in text:
        score += 50000
    if "merged" in text:
        score += 30000
    if "video" in text:
        score += 10000
    if "mp4" in text:
        score += 6000
    if "audio" in text:
        score -= 15000
    if "mp3" in text or "m4a" in text:
        score -= 30000
    if "thumbnail" in text or "poster" in text or "image" in text:
        score -= 40000
    return score


def audio_score(item: tuple[str, str]) -> int:
    url, trail = item
    text = f"{trail} {url}".lower()
    score = 0
    if "mp3" in text:
        score += 50000
    if "format=mp3" in text or "format%3dmp3" in text:
        score += 30000
    if "audio" in text:
        score += 20000
    if "m4a" in text:
        score += 12000
    if "video" in text or "merge" in text:
        score -= 8000
    if "thumbnail" in text or "poster" in text or "image" in text:
        score -= 40000
    return score


def get_json_sources() -> tuple[list[tuple[str, str]], list[str]]:
    payloads: list[tuple[str, Any]] = []
    errors: list[str] = []
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (Linux; Android 16) AppleWebKit/537.36 Chrome/138 Safari/537.36"})

    try:
        response = session.post(
            "https://cliplatch.com/api/parse",
            json={"url": SOURCE_URL},
            headers={"Accept": "application/json"},
            timeout=90,
        )
        print("ClipLatch status", response.status_code, flush=True)
        response.raise_for_status()
        payloads.append(("cliplatch", response.json()))
    except Exception as exc:  # noqa: BLE001
        errors.append(f"cliplatch: {exc}")

    for base in (
        "https://nativeig.vercel.app/api/video",
        "https://instagram-reels-downloader-tau.vercel.app/api/video",
    ):
        try:
            response = session.get(
                base,
                params={"postUrl": SOURCE_URL, "enhanced": "true"},
                headers={"Accept": "application/json"},
                timeout=60,
            )
            print(base, "status", response.status_code, flush=True)
            response.raise_for_status()
            payloads.append((base, response.json()))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{base}: {exc}")

    candidates = unique(
        [item for source, payload in payloads for item in collect_urls(payload, source)]
    )
    return candidates, errors


def download(url: str, destination: Path) -> None:
    headers = {"User-Agent": "Mozilla/5.0 (Linux; Android 16) AppleWebKit/537.36 Chrome/138 Safari/537.36"}
    hostname = urlparse(url).hostname or ""
    if hostname.endswith("cdninstagram.com") or hostname.endswith("fbcdn.net"):
        headers["Referer"] = "https://www.instagram.com/"
    with requests.get(url, headers=headers, stream=True, timeout=120, allow_redirects=True) as response:
        response.raise_for_status()
        total = 0
        with destination.open("wb") as handle:
            for chunk in response.iter_content(1024 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > 180 * 1024 * 1024:
                    raise RuntimeError("candidate exceeds 180 MiB")
                handle.write(chunk)
    if destination.stat().st_size < 1024:
        raise RuntimeError("candidate is too small")


def try_ytdlp() -> Path | None:
    template = str(DOWNLOADS / "ytdlp.%(ext)s")
    proc = run(
        [
            "yt-dlp",
            "--no-playlist",
            "--retries",
            "3",
            "--socket-timeout",
            "45",
            "--merge-output-format",
            "mp4",
            "--format",
            "bestvideo*+bestaudio/best",
            "--output",
            template,
            SOURCE_URL,
        ],
        check=False,
    )
    if proc.returncode != 0:
        return None
    for candidate in sorted(DOWNLOADS.glob("ytdlp.*"), key=lambda item: item.stat().st_size, reverse=True):
        probe = ffprobe(candidate)
        if has_video(probe) and has_audio(probe):
            return candidate
    return None


def try_candidates(candidates: list[tuple[str, str]]) -> Path:
    video_file: Path | None = None
    video_probe: dict[str, Any] | None = None
    failures: list[str] = []

    for index, (url, trail) in enumerate(sorted(candidates, key=video_score, reverse=True)[:40]):
        candidate = DOWNLOADS / f"video-{index}.bin"
        try:
            print("Trying video", video_score((url, trail)), trail, url[:500], flush=True)
            download(url, candidate)
            probe = ffprobe(candidate)
            if has_video(probe):
                video_file = candidate
                video_probe = probe
                print("Accepted video candidate", trail, candidate.stat().st_size, flush=True)
                break
            failures.append(f"{trail}: no video stream")
        except Exception as exc:  # noqa: BLE001
            candidate.unlink(missing_ok=True)
            failures.append(f"{trail}: {exc}")

    if video_file is None or video_probe is None:
        raise RuntimeError("No decodable video candidate: " + " | ".join(failures[:20]))

    if has_audio(video_probe):
        return video_file

    audio_file: Path | None = None
    for index, (url, trail) in enumerate(sorted(candidates, key=audio_score, reverse=True)[:40]):
        candidate = DOWNLOADS / f"audio-{index}.bin"
        try:
            print("Trying audio", audio_score((url, trail)), trail, url[:500], flush=True)
            download(url, candidate)
            probe = ffprobe(candidate)
            if has_audio(probe):
                audio_file = candidate
                print("Accepted audio candidate", trail, candidate.stat().st_size, flush=True)
                break
        except Exception as exc:  # noqa: BLE001
            candidate.unlink(missing_ok=True)
            failures.append(f"{trail}: {exc}")

    if audio_file is None:
        raise RuntimeError("Video was silent and no decodable audio candidate was found")

    muxed = DOWNLOADS / "muxed-source.mp4"
    run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(video_file),
            "-i",
            str(audio_file),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-shortest",
            "-movflags",
            "+faststart",
            str(muxed),
        ]
    )
    return muxed


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    if WORK.exists():
        shutil.rmtree(WORK)
    DOWNLOADS.mkdir(parents=True)

    source_candidate = try_ytdlp()
    resolution_errors: list[str] = []
    if source_candidate is None:
        candidates, resolution_errors = get_json_sources()
        print("Collected candidates", len(candidates), "errors", resolution_errors, flush=True)
        if not candidates:
            raise RuntimeError("No metadata candidates: " + " | ".join(resolution_errors))
        source_candidate = try_candidates(candidates)

    shutil.copyfile(source_candidate, SOURCE)
    source_probe = ffprobe(SOURCE)
    if not has_video(source_probe) or not has_audio(source_probe):
        raise RuntimeError("Resolved source does not contain both video and audio")

    filter_graph = (
        "[0:v]split=2[background][foreground];"
        "[background]scale=1280:720:force_original_aspect_ratio=increase,"
        "crop=1280:720,boxblur=24:12,eq=brightness=-0.16:saturation=0.86[blurred];"
        "[foreground]scale=1280:720:force_original_aspect_ratio=decrease[sharp];"
        "[blurred][sharp]overlay=(W-w)/2:(H-h)/2,format=yuv420p[vout]"
    )
    run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(SOURCE),
            "-filter_complex",
            filter_graph,
            "-map",
            "[vout]",
            "-an",
            "-r",
            "30",
            "-c:v",
            "libx264",
            "-profile:v",
            "high",
            "-level:v",
            "4.1",
            "-preset",
            "medium",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(VIDEO),
        ]
    )
    run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(SOURCE),
            "-map",
            "0:a:0",
            "-vn",
            "-af",
            "loudnorm=I=-16:TP=-1.5:LRA=11",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "192k",
            "-ar",
            "48000",
            "-ac",
            "2",
            str(AUDIO),
        ]
    )
    run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            "1",
            "-i",
            str(VIDEO),
            "-frames:v",
            "1",
            "-c:v",
            "libwebp",
            "-quality",
            "88",
            str(POSTER),
        ]
    )
    run(["ffmpeg", "-v", "error", "-i", str(VIDEO), "-f", "null", "-"])
    run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            "0.5",
            "-i",
            str(VIDEO),
            "-frames:v",
            "1",
            str(FRAME),
        ]
    )

    final_probe = ffprobe(VIDEO)
    video_streams = streams_of_kind(final_probe, "video")
    if not video_streams:
        raise RuntimeError("Final MP4 has no video stream")
    stream = video_streams[0]
    if stream.get("codec_name") != "h264" or stream.get("pix_fmt") != "yuv420p":
        raise RuntimeError(f"Final MP4 is not Android-safe H.264/yuv420p: {stream}")
    if FRAME.stat().st_size < 10_000:
        raise RuntimeError("Final MP4 did not yield a real validation frame")

    manifest = {
        "source": SOURCE_URL,
        "sourceProbe": source_probe,
        "finalProbe": final_probe,
        "resolutionErrors": resolution_errors,
        "files": {
            path.name: {"bytes": path.stat().st_size, "sha256": sha256(path)}
            for path in (VIDEO, AUDIO, POSTER, FRAME)
        },
    }
    MANIFEST.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest["files"], indent=2), flush=True)


if __name__ == "__main__":
    main()

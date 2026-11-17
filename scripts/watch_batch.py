"""Watch an overnight batch, retry failed parts, and save verified local downloads."""

import argparse
import datetime
import json
import os
import shutil
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def save_downloads(snapshot, directory):
    archive = Path(snapshot["archive_path"]).resolve()
    if not archive.is_relative_to((ROOT / "storage" / "exports").resolve()) or not archive.is_file():
        raise ValueError("The batch archive is missing or outside the export folder")
    with zipfile.ZipFile(archive) as bundle:
        corrupt = bundle.testzip()
        if corrupt:
            raise RuntimeError(f"Archive verification failed: {corrupt}")
        videos = [name for name in bundle.namelist() if name.endswith(".mp4")]
        if snapshot["status"] == "completed" and len(videos) != len(snapshot["clips"]):
            raise RuntimeError("The archive does not contain every completed video")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    files = []
    for source in sorted(archive.parent.iterdir()):
        if source.name == "download_receipt.json" or not source.is_file() or source.suffix not in {".mp4", ".srt", ".txt", ".json", ".zip"}:
            continue
        target = directory / source.name
        if target.exists():
            if os.path.samefile(source, target):
                files.append(str(target))
                continue
            raise FileExistsError(f"A different download already exists: {target}")
        try:
            os.link(source, target)
        except OSError:
            shutil.copy2(source, target)
        files.append(str(target))
    receipt = {"batch_id": snapshot["id"], "status": snapshot["status"], "archive_verified": True,
               "video_count": len(videos), "saved_at": datetime.datetime.now().isoformat(),
               "download_directory": str(directory), "files": files}
    (directory / "download_receipt.json").write_text(json.dumps(receipt, indent=2))
    (archive.parent / "download_receipt.json").write_text(json.dumps(receipt, indent=2))
    return receipt


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch", required=True)
    parser.add_argument("--download-dir", required=True)
    arguments = parser.parse_args()
    endpoint = f"http://localhost:8000/api/video/batch/{arguments.batch}"
    retries = 0
    deadline = time.monotonic() + 24 * 3600
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(endpoint, timeout=15) as response:
                snapshot = json.load(response)
            states = " | ".join(f"Part {clip['index']}: {clip.get('progress', 0)}% {clip.get('stage', '')}" for clip in snapshot["clips"])
            print(f"{datetime.datetime.now().isoformat(timespec='seconds')} [{snapshot['status']}] {states}", flush=True)
            if snapshot["status"] in {"failed", "completed_with_errors"} and retries < 2:
                retries += 1
                time.sleep(30 * retries)
                request = urllib.request.Request(endpoint + "/resume", data=b"", method="POST")
                with urllib.request.urlopen(request, timeout=30):
                    pass
            elif snapshot["status"] in {"completed", "completed_with_errors"}:
                receipt = save_downloads(snapshot, arguments.download_dir)
                print(json.dumps(receipt, indent=2), flush=True)
                return 0 if snapshot["status"] == "completed" else 2
            elif snapshot["status"] == "failed":
                raise RuntimeError(snapshot.get("error") or "Batch processing failed after retries")
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            print(f"Waiting for the local server: {error}", flush=True)
        time.sleep(30)
    raise TimeoutError("The batch did not finish within 24 hours")


if __name__ == "__main__":
    raise SystemExit(main())

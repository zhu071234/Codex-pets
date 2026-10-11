"""Crop and preview an encoded v2 pet atlas without changing any pet pixels."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw

CELL_WIDTH = 192
CELL_HEIGHT = 208
BACKGROUND = (242, 242, 242)
ROW_DURATIONS = {
    "idle": [280, 110, 110, 140, 140, 320],
    "running-right": [120, 120, 120, 120, 120, 120, 120, 220],
    "running-left": [120, 120, 120, 120, 120, 120, 120, 220],
    "waving": [140, 140, 140, 280],
    "jumping": [140, 140, 140, 140, 280],
    "failed": [140, 140, 140, 140, 140, 140, 140, 240],
    "waiting": [150, 150, 150, 150, 150, 260],
    "running": [120, 120, 120, 120, 120, 220],
    "review": [150, 150, 150, 150, 150, 280],
}
LOOK_LABELS = [
    "000", "022.5", "045", "067.5", "090", "112.5", "135", "157.5",
    "180", "202.5", "225", "247.5", "270", "292.5", "315", "337.5",
]


def crop_cell(atlas, row, column):
    box = (
        column * CELL_WIDTH, row * CELL_HEIGHT,
        (column + 1) * CELL_WIDTH, (row + 1) * CELL_HEIGHT,
    )
    cell = atlas.crop(box)
    if cell.getchannel("A").getbbox() is None:
        raise ValueError("required atlas cell is empty: row=%s column=%s" % (row, column))
    return cell, box


def matte(cell):
    canvas = Image.new("RGBA", cell.size, BACKGROUND + (255,))
    canvas.alpha_composite(cell)
    return canvas.convert("RGB")


def save_gif(cells, durations, destination):
    """Use one palette across the sequence; pet cells remain at native bounds."""
    frames = [matte(cell) for cell in cells]
    samples = Image.new("RGB", (CELL_WIDTH * len(frames), CELL_HEIGHT), BACKGROUND)
    for index, frame in enumerate(frames):
        samples.paste(frame, (index * CELL_WIDTH, 0))
    palette = samples.quantize(colors=256)
    encoded = [frame.quantize(palette=palette) for frame in frames]
    encoded[0].save(
        destination, format="GIF", save_all=True, append_images=encoded[1:],
        duration=durations, loop=0, disposal=2, optimize=False,
    )
    with Image.open(destination) as opened:
        if opened.size != (CELL_WIDTH, CELL_HEIGHT):
            raise RuntimeError("GIF changed the native cell bounds: " + str(destination))
        actual_duration = sum(
            _duration_at(opened, index) for index in range(opened.n_frames)
        )
    if actual_duration != sum(durations):
        raise RuntimeError("GIF timing changed: " + str(destination))
    return {
        "file": str(destination), "source_frames": len(cells),
        "encoded_frames": opened.n_frames, "duration_ms": actual_duration,
        "native_cell_size": [CELL_WIDTH, CELL_HEIGHT],
    }


def _duration_at(image, index):
    image.seek(index)
    return image.info.get("duration", 0)


def save_mp4(cells, durations, destination, ffmpeg):
    """Use a 100 Hz timeline to preserve the provided 10 ms GIF timings."""
    fps = 100
    if any(duration % 10 for duration in durations):
        raise ValueError("MP4 timeline requires durations that are multiples of 10 ms")
    log_path = destination.with_suffix(".ffmpeg.log")
    command = [
        str(ffmpeg), "-loglevel", "error", "-y",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "192x208",
        "-framerate", str(fps), "-i", "pipe:0", "-an", "-c:v", "libx264",
        "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
        "-movflags", "+faststart", str(destination),
    ]
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    with log_path.open("wb") as log:
        process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=log, creationflags=creationflags,
        )
        try:
            for cell, duration in zip(cells, durations):
                data = matte(cell).tobytes()
                for unused in range(duration // 10):
                    process.stdin.write(data)
            process.stdin.close()
            code = process.wait(timeout=120)
        except (BrokenPipeError, OSError) as exc:
            process.kill()
            process.wait()
            log.flush()
            detail = log_path.read_text(encoding="utf-8", errors="replace")
            raise RuntimeError("MP4 input pipe failed: %s\n%s" % (exc, detail)) from exc
        except BaseException:
            process.kill()
            process.wait()
            raise
    if code != 0 or not destination.is_file() or destination.stat().st_size == 0:
        detail = log_path.read_text(encoding="utf-8", errors="replace")
        raise RuntimeError("MP4 encoding failed (exit %s): %s" % (code, detail))
    return {
        "file": str(destination), "encoder": str(ffmpeg), "fps": fps,
        "duration_ms": sum(durations), "video_frames": sum(durations) // 10,
        "native_cell_size": [CELL_WIDTH, CELL_HEIGHT], "codec": "h264",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("atlas", help="Exact final encoded PNG or WebP atlas")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--ffmpeg", help="Existing ffmpeg executable; otherwise resolve PATH")
    args = parser.parse_args()
    atlas_path = Path(args.atlas).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    source_bytes = atlas_path.read_bytes()
    source_digest = hashlib.sha256(source_bytes).hexdigest()
    with Image.open(atlas_path) as opened:
        if opened.format not in {"PNG", "WEBP"}:
            raise ValueError("final atlas must be encoded PNG or WebP")
        atlas = opened.convert("RGBA")
    if atlas.size != (1536, 2288):
        raise ValueError("v2 atlas must be 1536x2288; got %sx%s" % atlas.size)
    if atlas.getchannel("A").getextrema()[0] == 255:
        raise ValueError("atlas has no transparent background")
    output_dir.mkdir(parents=True, exist_ok=True)
    frames_root = output_dir / "final-frames"
    frames_root.mkdir(exist_ok=True)
    states = {}
    records = []
    manifest_rows = []
    all_cells = []
    all_durations = []
    timeline = []
    elapsed = 0
    for row, (state, durations) in enumerate(ROW_DURATIONS.items()):
        state_dir = frames_root / state
        state_dir.mkdir(exist_ok=True)
        cells = []
        paths = []
        for frame_index, duration in enumerate(durations):
            cell, box = crop_cell(atlas, row, frame_index)
            path = state_dir / ("%02d.png" % frame_index)
            cell.save(path)
            with Image.open(path) as saved:
                if saved.convert("RGBA").tobytes() != cell.tobytes():
                    raise RuntimeError("export changed cell pixels: " + str(path))
            paths.append(str(path))
            cells.append(cell)
            record = {
                "state": state, "row": row, "frame": frame_index,
                "source_box": list(box), "file": str(path), "duration_ms": duration,
                "rgba_sha256": hashlib.sha256(cell.tobytes()).hexdigest(),
            }
            records.append(record)
            timeline.append(dict(record, start_ms=elapsed))
            elapsed += duration
        states[state] = cells
        manifest_rows.append({"state": state, "frames": paths, "method": "exact-atlas-cell-crop"})
        all_cells.extend(cells)
        all_durations.extend(durations)

    look_dir = frames_root / "look"
    look_dir.mkdir(exist_ok=True)
    look_cells = []
    for index, label in enumerate(LOOK_LABELS):
        row, column = 9 + index // 8, index % 8
        cell, box = crop_cell(atlas, row, column)
        path = look_dir / (label + ".png")
        cell.save(path)
        look_cells.append(cell)
        records.append({
            "state": "look", "direction": label, "row": row, "frame": column,
            "source_box": list(box), "file": str(path), "duration_ms": 180,
            "rgba_sha256": hashlib.sha256(cell.tobytes()).hexdigest(),
        })
    manifest = {
        "ok": True, "source_atlas": str(atlas_path), "source_sha256": source_digest,
        "cell_size": [CELL_WIDTH, CELL_HEIGHT], "rows": manifest_rows,
    }
    (frames_root / "frames-manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8",
    )
    previews = []
    for state, durations in ROW_DURATIONS.items():
        previews.append(save_gif(states[state], durations, output_dir / (state + ".gif")))
    previews.append(save_gif(all_cells, all_durations, output_dir / "all-states.gif"))
    transition_cells = states["idle"] + states["jumping"] + states["idle"]
    transition_durations = ROW_DURATIONS["idle"] + ROW_DURATIONS["jumping"] + ROW_DURATIONS["idle"]
    previews.append(save_gif(transition_cells, transition_durations, output_dir / "idle-jump-idle.gif"))
    previews.append(save_gif(look_cells, [180] * 16, output_dir / "look-loop.gif"))

    # Labels are outside native 192x208 cells; no label touches or redraws the sprite.
    selected = [("idle", 0), ("jumping", 2), ("running", 2), ("review", 2)]
    label_height = 28
    still_strip = Image.new("RGB", (CELL_WIDTH * len(selected), CELL_HEIGHT + label_height), BACKGROUND)
    draw = ImageDraw.Draw(still_strip)
    still_records = []
    for column, (state, frame_index) in enumerate(selected):
        left = column * CELL_WIDTH
        label = "%s / frame %02d" % (state, frame_index)
        draw.text((left + 6, 8), label, fill=(20, 20, 20))
        still_strip.paste(matte(states[state][frame_index]), (left, label_height))
        record = next(item for item in timeline if item["state"] == state and item["frame"] == frame_index)
        still_records.append(dict(record, strip_box=[left, label_height, left + CELL_WIDTH, label_height + CELL_HEIGHT]))
    still_path = output_dir / "motion-stills.png"
    still_strip.save(still_path)

    ffmpeg = Path(args.ffmpeg).expanduser().resolve() if args.ffmpeg else shutil.which("ffmpeg")
    errors = []
    video = None
    if not ffmpeg:
        errors.append("No existing ffmpeg executable supplied or available on PATH; MP4 not encoded.")
    else:
        try:
            video = save_mp4(all_cells, all_durations, output_dir / "all-states.mp4", ffmpeg)
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            errors.append(str(exc))
    if atlas_path.read_bytes() != source_bytes:
        errors.append("Source atlas bytes changed while rendering previews.")
    report = {
        "ok": not errors, "source_atlas": str(atlas_path), "source_sha256": source_digest,
        "source_encoded_bytes": len(source_bytes), "frames_root": str(frames_root),
        "cell_size": [CELL_WIDTH, CELL_HEIGHT], "background_rgb": list(BACKGROUND),
        "frames": records, "all_states_timeline": timeline, "previews": previews,
        "video": video, "still_strip": {"file": str(still_path), "frames": still_records},
        "errors": errors,
    }
    report_path = output_dir / "preview-manifest.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "ok": report["ok"], "report": str(report_path), "frames_root": str(frames_root),
        "gif_count": len(previews), "mp4": video, "errors": errors,
    }, indent=2))
    raise SystemExit(0 if report["ok"] else 1)


if __name__ == "__main__":
    main()

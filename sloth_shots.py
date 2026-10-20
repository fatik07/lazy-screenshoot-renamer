"""sloth-shots: rename your screenshots by what's in them, so you never have to.

Uses an open-weight vision model (Gemma 4 by default) through OpenRouter.
Zero third-party dependencies.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

API_URL = "https://openrouter.ai/api/v1/chat/completions"
# paid Gemma first (about $0.00003 per screenshot, 1-2s), free tier as fallback
DEFAULT_MODELS = "google/gemma-4-31b-it,google/gemma-4-26b-a4b-it,google/gemma-4-31b-it:free"
DEFAULT_PREFIXES = ("Screenshot", "Screen Shot")
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".heic", ".webp"}
LOG_NAME = ".sloth-shots.jsonl"
CACHE_NAME = ".sloth-shots-cache.json"
MAX_SIDE = 1024

PROMPT = """Look at this screenshot and write a short filename for it.

Rules:
- 3 to 6 words, lowercase, words joined with hyphens
- name the app or website if you can tell, then the main subject
- be specific: "vscode-python-import-error" beats "code-editor"
- no dates, no file extension, no quotes, no explanation

Reply with the filename only."""


class DailyLimitReached(Exception):
    pass


# --- model ------------------------------------------------------------------


def shrink(path: Path) -> tuple[bytes, str]:
    """Return the smaller of the original and a MAX_SIDE JPEG made with macOS sips."""
    original = path.read_bytes()
    mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    if shutil.which("sips"):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "shot.jpg"
            result = subprocess.run(
                ["sips", "-s", "format", "jpeg", "-Z", str(MAX_SIDE), str(path), "--out", str(out)],
                capture_output=True,
            )
            if result.returncode == 0 and out.exists() and out.stat().st_size < len(original):
                return out.read_bytes(), "image/jpeg"
    return original, mime


def free_quota(api_key: str) -> int | None:
    """Free-model requests left today, or None if OpenRouter doesn't say."""
    req = urllib.request.Request("https://openrouter.ai/api/v1/key", headers={"Authorization": f"Bearer {api_key}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.load(resp)["data"]
    except (urllib.error.URLError, TimeoutError, KeyError, ValueError):
        return None
    daily = data.get("free_model_daily_requests") or {}
    return daily.get("remaining")


def ask_model(path: Path, models: list[str], api_key: str, rounds: int = 4) -> tuple[str, str]:
    """Return (reply, model). Free models share an upstream pool, so when one is
    busy we move on to the next instead of waiting."""
    data, mime = shrink(path)
    image = f"data:{mime};base64,{base64.b64encode(data).decode()}"

    delay = 10.0
    for _ in range(rounds):
        for model in models:
            payload = {
                "model": model,
                "temperature": 0.2,
                "max_tokens": 100,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": PROMPT},
                            {"type": "image_url", "image_url": {"url": image}},
                        ],
                    }
                ],
            }
            req = urllib.request.Request(
                API_URL,
                data=json.dumps(payload).encode(),
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "X-Title": "sloth-shots",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    body = json.load(resp)
                return body["choices"][0]["message"]["content"] or "", model
            except urllib.error.HTTPError as e:
                detail = e.read().decode(errors="replace")
                if e.code == 429:
                    if model.endswith(":free") and "upstream" not in detail and free_quota(api_key) == 0:
                        if all(m.endswith(":free") for m in models):
                            raise DailyLimitReached(detail) from e
                        continue
                    print(f"  {model} is busy, trying another...", file=sys.stderr)
                    continue
                if e.code == 402:
                    print(f"  {model} needs OpenRouter credits, trying another...", file=sys.stderr)
                    continue
                if e.code >= 500:
                    print(f"  {model} returned {e.code}, trying another...", file=sys.stderr)
                    continue
                raise RuntimeError(f"OpenRouter error {e.code}: {detail}") from e
            except (urllib.error.URLError, TimeoutError) as e:
                print(f"  network error: {e}", file=sys.stderr)
                continue
        print(f"  all models busy, waiting {delay:.0f}s...", file=sys.stderr)
        time.sleep(delay)
        delay = min(delay * 2, 60)
    raise RuntimeError("all models stayed busy; try again later")


# --- naming -----------------------------------------------------------------


def slugify(text: str) -> str:
    # prefer an already-hyphenated answer buried in chatty output
    hyphenated = re.search(r"[a-z0-9]+(?:-[a-z0-9]+)+", text.lower())
    if hyphenated:
        return "-".join(hyphenated.group().split("-")[:8])
    line = next((l for l in text.strip().splitlines() if l.strip()), "")
    line = re.sub(r"\.(png|jpe?g|heic|webp)$", "", line.strip().strip("`\"'"), flags=re.I)
    slug = re.sub(r"[^a-z0-9]+", "-", line.lower()).strip("-")
    words = [w for w in slug.split("-") if w][:8]
    return "-".join(words)


def shot_date(path: Path) -> str:
    """Pull the date out of the original filename, falling back to mtime."""
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", path.name)
    if m:
        return "-".join(m.groups())
    return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d")


def target_for(path: Path, slug: str) -> Path:
    base = f"{shot_date(path)}_{slug}"
    ext = path.suffix.lower()
    candidate = path.with_name(base + ext)
    n = 2
    while candidate.exists():
        candidate = path.with_name(f"{base}-{n}{ext}")
        n += 1
    return candidate


def is_screenshot(path: Path, prefixes: tuple[str, ...]) -> bool:
    return (
        path.is_file()
        and not path.name.startswith(".")
        and path.suffix.lower() in IMAGE_EXTS
        and path.name.startswith(prefixes)
    )


# --- log --------------------------------------------------------------------


def log_path(folder: Path) -> Path:
    return folder / LOG_NAME


def append_log(folder: Path, entry: dict) -> None:
    with log_path(folder).open("a") as f:
        f.write(json.dumps(entry) + "\n")


def read_log(folder: Path) -> list[dict]:
    p = log_path(folder)
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def load_cache(folder: Path) -> dict[str, str]:
    p = folder / CACHE_NAME
    return json.loads(p.read_text()) if p.exists() else {}


def save_cache(folder: Path, cache: dict[str, str]) -> None:
    (folder / CACHE_NAME).write_text(json.dumps(cache, indent=1))


# --- commands ---------------------------------------------------------------


def rename_one(path: Path, args, apply: bool) -> Path | None:
    # dry runs cache their answers so --apply doesn't spend the free quota twice
    cache = load_cache(path.parent)
    started = time.monotonic()
    model = "cache"
    slug = cache.get(path.name)
    if not slug:
        reply, model = ask_model(path, args.models, args.api_key)
        slug = slugify(reply)
    took = time.monotonic() - started
    if slug.count("-") < 1:
        print(f"{path.name}\n  skipped: model gave no usable name ({slug or 'empty'!r})")
        return None
    target = target_for(path, slug)
    arrow = "->" if apply else "~>"
    print(f"{path.name}\n  {arrow} {target.name}  ({took:.1f}s)")
    if not apply:
        cache[path.name] = slug
        save_cache(path.parent, cache)
        return None
    if target.exists():  # re-check right before the move; never overwrite
        print("  skipped: target appeared meanwhile", file=sys.stderr)
        return None
    path.rename(target)
    append_log(
        path.parent,
        {"at": datetime.now().isoformat(timespec="seconds"), "old": path.name, "new": target.name, "model": model},
    )
    if cache.pop(path.name, None):
        save_cache(path.parent, cache)
    return target


def cmd_tidy(args) -> int:
    folder = Path(args.folder).expanduser()
    shots = sorted(p for p in folder.iterdir() if is_screenshot(p, args.prefixes))
    if args.limit:
        shots = shots[: args.limit]
    cached = load_cache(folder)
    calls = sum(1 for p in shots if p.name not in cached)
    # the daily cap only applies when every model is a :free one
    all_free = all(m.endswith(":free") for m in args.models)
    left = free_quota(args.api_key) if calls and all_free else None
    if left is not None:
        print(f"Free requests left today: {left}")
        if calls > left:
            # keep cached files plus as many new ones as today's quota allows
            budget, kept = left, []
            for p in shots:
                if p.name in cached:
                    kept.append(p)
                elif budget > 0:
                    kept.append(p)
                    budget -= 1
            print(f"Only processing {len(kept)} of {len(shots)} today; run again tomorrow for the rest.")
            shots = kept
    if not shots:
        print("No screenshots to rename.")
        return 0
    mode = "Renaming" if args.apply else "Dry run (nothing is renamed; add --apply)"
    print(f"{mode}: {len(shots)} screenshot(s) in {folder}\n")
    done = 0
    for p in shots:
        try:
            if rename_one(p, args, args.apply):
                done += 1
        except DailyLimitReached:
            print("\nOpenRouter's daily free limit is reached. Run again tomorrow to continue.", file=sys.stderr)
            break
        except RuntimeError as e:
            print(f"  failed: {e}", file=sys.stderr)
        if args.delay:
            time.sleep(args.delay)
    if args.apply:
        print(f"\nRenamed {done} file(s). Undo with: sloth-shots undo {args.folder}")
    return 0


def cmd_watch(args) -> int:
    folder = Path(args.folder).expanduser()
    print(f"Watching {folder} for new screenshots. Ctrl+C to stop.")
    seen = {p for p in folder.iterdir() if is_screenshot(p, args.prefixes)}
    sizes: dict[Path, int] = {}
    try:
        while True:
            for p in folder.iterdir():
                if p in seen or not is_screenshot(p, args.prefixes):
                    continue
                size = p.stat().st_size
                # wait until the file stops growing (macOS writes it in steps)
                if sizes.get(p) != size or size == 0:
                    sizes[p] = size
                    continue
                seen.add(p)
                sizes.pop(p, None)
                try:
                    rename_one(p, args, apply=True)
                except DailyLimitReached:
                    print("Daily free limit reached; leaving new screenshots as they are.", file=sys.stderr)
                except RuntimeError as e:
                    print(f"  failed: {e}", file=sys.stderr)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nBye. Back to napping.")
    return 0


def cmd_undo(args) -> int:
    folder = Path(args.folder).expanduser()
    entries = read_log(folder)
    if not entries:
        print("Nothing to undo.")
        return 0
    restored, kept = 0, []
    for e in reversed(entries):
        new, old = folder / e["new"], folder / e["old"]
        if new.exists() and not old.exists():
            new.rename(old)
            restored += 1
        else:
            kept.append(e)
    log_path(folder).write_text("".join(json.dumps(e) + "\n" for e in reversed(kept)))
    print(f"Restored {restored} original name(s).")
    return 0


def cmd_stats(args) -> int:
    folder = Path(args.folder).expanduser()
    n = len(read_log(folder))
    saved = n * args.seconds_per_file
    left = sum(1 for p in folder.iterdir() if is_screenshot(p, args.prefixes))
    print(f"Screenshots renamed:   {n}")
    print(f"Still unnamed:         {left}")
    print(f"Effort saved (est.):   {saved / 60:.0f} min  ({args.seconds_per_file}s per manual rename)")
    return 0


# --- cli --------------------------------------------------------------------


def default_folder() -> str:
    try:
        out = subprocess.run(
            ["defaults", "read", "com.apple.screencapture", "location"], capture_output=True, text=True
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except FileNotFoundError:
        pass
    return "~/Desktop"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sloth-shots", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    def common(p, needs_model=True):
        p.add_argument("folder", nargs="?", default=default_folder(), help="screenshot folder (default: macOS screenshot location)")
        p.add_argument("--prefix", dest="prefixes", action="append", help="filename prefix to treat as a screenshot (repeatable)")
        if needs_model:
            p.add_argument("--model", default=os.environ.get("SLOTH_MODEL", DEFAULT_MODELS),
                           help="model id, or several comma-separated to fall back through")

    p = sub.add_parser("tidy", help="rename existing screenshots (dry run unless --apply)")
    common(p)
    p.add_argument("--apply", action="store_true", help="actually rename files")
    p.add_argument("--limit", type=int, default=0, help="only process the first N")
    p.add_argument("--delay", type=float, default=3.0, help="seconds between requests (free tier is rate limited)")
    p.set_defaults(func=cmd_tidy)

    p = sub.add_parser("watch", help="rename new screenshots as they appear")
    common(p)
    p.add_argument("--interval", type=float, default=2.0)
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("undo", help="restore original names")
    common(p, needs_model=False)
    p.set_defaults(func=cmd_undo)

    p = sub.add_parser("stats", help="how much effort you've saved")
    common(p, needs_model=False)
    p.add_argument("--seconds-per-file", type=int, default=20)
    p.set_defaults(func=cmd_stats)

    args = parser.parse_args(argv)
    args.prefixes = tuple(args.prefixes or DEFAULT_PREFIXES)
    if hasattr(args, "model"):
        args.models = [m.strip() for m in args.model.split(",") if m.strip()]
    if args.cmd in ("tidy", "watch"):
        args.api_key = os.environ.get("OPENROUTER_API_KEY", "")
        if not args.api_key:
            parser.error("set OPENROUTER_API_KEY (free key at https://openrouter.ai/keys)")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

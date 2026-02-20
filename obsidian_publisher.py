#!/usr/bin/env python3
"""
obsidian_publisher.py
Watches an Obsidian publish folder, processes markdown files,
uploads images to Cloudinary, and deploys to GitHub Pages.
"""

import os
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

import cloudinary
import cloudinary.uploader
import yaml
from dotenv import load_dotenv
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

# Load secrets from .env (never committed to git)
load_dotenv(Path(__file__).parent / ".env")

# ---------------------------------------------------------------------------
# Configuration — fill these in before running
# ---------------------------------------------------------------------------
VAULT_PUBLISH_DIR    = Path("/Users/jalen/Library/Mobile Documents/iCloud~md~obsidian/Documents/Jalen's Notes/51- Publish")   # Obsidian folder you drop posts into
VAULT_ATTACHMENTS_DIR = Path("/Users/jalen/Library/Mobile Documents/iCloud~md~obsidian/Documents/Jalen's Notes/51- Publish/attachments")  # Where Obsidian stores attachments
POSTS_DIR            = Path("/Users/jalen/Projects/jhlj.studio/_posts")   # _posts/ inside your Jekyll repo
REPO_DIR             = Path("/Users/jalen/Projects/jhlj.studio")   # Root of your GitHub Pages git repo

CLOUDINARY_CLOUD_NAME = os.environ["CLOUDINARY_CLOUD_NAME"]
CLOUDINARY_API_KEY    = os.environ["CLOUDINARY_API_KEY"]
CLOUDINARY_API_SECRET = os.environ["CLOUDINARY_API_SECRET"]

# Subfolder inside VAULT_PUBLISH_DIR where processed files are moved
PUBLISHED_DIR = VAULT_PUBLISH_DIR / "published"

# Seconds to wait after a file event before processing (debounce)
DEBOUNCE_SECONDS = 3
# ---------------------------------------------------------------------------

cloudinary.config(
    cloud_name=CLOUDINARY_CLOUD_NAME,
    api_key=CLOUDINARY_API_KEY,
    api_secret=CLOUDINARY_API_SECRET,
    secure=True,
)


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------

def log_ok(msg: str) -> None:
    print(f"  ✓  {msg}", flush=True)


def log_err(msg: str) -> None:
    print(f"  ✗  {msg}", flush=True)


def log_info(msg: str) -> None:
    print(f"     {msg}", flush=True)


# ---------------------------------------------------------------------------
# Front-matter helpers
# ---------------------------------------------------------------------------

FRONT_MATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def parse_or_generate_front_matter(source_path: Path, body: str) -> tuple[dict, str]:
    """
    Return (front_matter_dict, body_without_front_matter).
    If front matter is missing, generate sensible defaults.
    """
    match = FRONT_MATTER_RE.match(body)
    if match:
        fm = yaml.safe_load(match.group(1)) or {}
        body = body[match.end():]
    else:
        fm = {}

    # Ensure required keys exist
    if "title" not in fm:
        fm["title"] = source_path.stem.replace("-", " ").replace("_", " ").title()

    if "date" not in fm:
        fm["date"] = datetime.now().strftime("%Y-%m-%d")

    if "layout" not in fm:
        fm["layout"] = "post"

    return fm, body


def render_front_matter(fm: dict) -> str:
    return "---\n" + yaml.dump(fm, default_flow_style=False, allow_unicode=True) + "---\n\n"


# ---------------------------------------------------------------------------
# Markdown transformation helpers
# ---------------------------------------------------------------------------

WIKI_LINK_RE   = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")
OBSIDIAN_IMG_RE = re.compile(r"!\[\[([^\]]+)\]\]")
MARKDOWN_IMG_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")


def strip_wiki_links(text: str) -> str:
    """Replace [[Page|alias]] or [[Page]] with alias or Page (plain text)."""
    def replacer(m: re.Match) -> str:
        alias = m.group(2)
        page  = m.group(1)
        return alias if alias else page

    return WIKI_LINK_RE.sub(replacer, text)


def find_attachment(filename: str) -> Path | None:
    """Search VAULT_ATTACHMENTS_DIR recursively for a file by name."""
    candidates = list(VAULT_ATTACHMENTS_DIR.rglob(filename))
    if candidates:
        return candidates[0]
    return None


def upload_image(local_path: Path) -> str | None:
    """
    Upload an image to Cloudinary (overwrite=False — skip if already exists).
    Returns the secure CDN URL or None on failure.
    """
    public_id = f"blog/{local_path.stem}"
    try:
        result = cloudinary.uploader.upload(
            str(local_path),
            public_id=public_id,
            overwrite=False,
            unique_filename=False,
            resource_type="image",
        )
        url = result.get("secure_url")
        log_ok(f"Cloudinary → {url}")
        return url
    except Exception as exc:
        log_err(f"Cloudinary upload failed for {local_path.name}: {exc}")
        return None


def process_images(text: str) -> str:
    """
    Find all image references (![[...]] and ![]()), upload each to Cloudinary,
    and rewrite paths to the CDN URL.
    """
    # --- Obsidian embeds: ![[image.png]] ---
    def replace_obsidian_img(m: re.Match) -> str:
        filename = m.group(1).strip()
        local    = find_attachment(filename)
        if local is None:
            log_err(f"Attachment not found: {filename}")
            return m.group(0)  # leave as-is
        url = upload_image(local)
        if url is None:
            return m.group(0)
        alt = Path(filename).stem
        return f"![{alt}]({url})"

    text = OBSIDIAN_IMG_RE.sub(replace_obsidian_img, text)

    # --- Standard markdown images: ![alt](path) ---
    def replace_md_img(m: re.Match) -> str:
        alt  = m.group(1)
        path = m.group(2).strip()

        # Skip already-CDN or external URLs
        if path.startswith("http://") or path.startswith("https://"):
            return m.group(0)

        local = find_attachment(Path(path).name)
        if local is None:
            # Try the path as-is relative to the vault
            candidate = VAULT_ATTACHMENTS_DIR / path
            if candidate.exists():
                local = candidate

        if local is None:
            log_err(f"Image not found: {path}")
            return m.group(0)

        url = upload_image(local)
        if url is None:
            return m.group(0)
        return f"![{alt}]({url})"

    text = MARKDOWN_IMG_RE.sub(replace_md_img, text)
    return text


# ---------------------------------------------------------------------------
# Jekyll filename helpers
# ---------------------------------------------------------------------------

SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    return SLUG_RE.sub("-", text.lower()).strip("-")


def build_jekyll_filename(fm: dict, source_stem: str) -> str:
    """Return a Jekyll-style YYYY-MM-DD-slug.md filename."""
    date_str = str(fm.get("date", datetime.now().strftime("%Y-%m-%d")))[:10]
    slug     = slugify(fm.get("title", source_stem))
    return f"{date_str}-{slug}.md"


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def git_commit_and_push(post_path: Path) -> bool:
    """Stage only _posts/, commit, and push. Returns True on success."""
    try:
        rel = post_path.relative_to(REPO_DIR)
        subprocess.run(
            ["git", "add", str(rel)],
            cwd=REPO_DIR, check=True, capture_output=True,
        )
        commit_msg = f"publish: {post_path.name}"
        result = subprocess.run(
            ["git", "commit", "-m", commit_msg],
            cwd=REPO_DIR, capture_output=True, text=True,
        )
        if result.returncode != 0:
            # Nothing to commit is not a real error
            if "nothing to commit" in result.stdout + result.stderr:
                log_info("Nothing new to commit (file already up to date).")
                return True
            log_err(f"git commit failed: {result.stderr.strip()}")
            return False

        subprocess.run(
            ["git", "push"],
            cwd=REPO_DIR, check=True, capture_output=True,
        )
        log_ok(f"Pushed {post_path.name} to GitHub Pages")
        return True

    except subprocess.CalledProcessError as exc:
        log_err(f"git error: {exc.stderr.decode().strip() if exc.stderr else exc}")
        return False


# ---------------------------------------------------------------------------
# Core processing pipeline
# ---------------------------------------------------------------------------

def process_file(source_path: Path) -> None:
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Processing: {source_path.name}")

    try:
        raw = source_path.read_text(encoding="utf-8")
    except OSError as exc:
        log_err(f"Could not read file: {exc}")
        return

    # 1. Parse / generate front matter
    fm, body = parse_or_generate_front_matter(source_path, raw)
    log_ok("Front matter ready")

    # 2. Strip wiki links
    body = strip_wiki_links(body)
    log_ok("Wiki links stripped")

    # 3. Process and upload images
    body = process_images(body)
    log_ok("Images processed")

    # 4. Build the final post content
    output = render_front_matter(fm) + body

    # 5. Write to _posts/ with Jekyll filename
    jekyll_name = build_jekyll_filename(fm, source_path.stem)
    post_path   = POSTS_DIR / jekyll_name
    POSTS_DIR.mkdir(parents=True, exist_ok=True)
    post_path.write_text(output, encoding="utf-8")
    log_ok(f"Written → {post_path}")

    # 6. Commit and push
    success = git_commit_and_push(post_path)
    if not success:
        log_err("Deploy failed — original file left in place")
        return

    # 7. Move original to published/
    PUBLISHED_DIR.mkdir(parents=True, exist_ok=True)
    dest = PUBLISHED_DIR / source_path.name
    shutil.move(str(source_path), dest)
    log_ok(f"Moved original → {dest}")


# ---------------------------------------------------------------------------
# Watchdog handler with debounce
# ---------------------------------------------------------------------------

class MarkdownHandler(FileSystemEventHandler):
    def __init__(self) -> None:
        super().__init__()
        # Map path → timestamp of last event
        self._pending: dict[str, float] = {}

    def _schedule(self, path: str) -> None:
        self._pending[path] = time.monotonic()

    def on_created(self, event) -> None:
        if not event.is_directory and event.src_path.endswith(".md"):
            self._schedule(event.src_path)

    def on_modified(self, event) -> None:
        if not event.is_directory and event.src_path.endswith(".md"):
            self._schedule(event.src_path)

    def on_moved(self, event) -> None:
        if not event.is_directory and event.dest_path.endswith(".md"):
            self._schedule(event.dest_path)

    def flush_pending(self) -> None:
        """Called on each poll tick — fire handlers whose debounce has elapsed."""
        now  = time.monotonic()
        done = []
        for path, ts in list(self._pending.items()):
            if now - ts >= DEBOUNCE_SECONDS:
                done.append(path)

        for path in done:
            del self._pending[path]
            p = Path(path)
            # Skip files already inside the published/ subdirectory
            if p.parent == PUBLISHED_DIR:
                continue
            if p.exists():
                process_file(p)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    print("=" * 60)
    print(" Obsidian → GitHub Pages publisher")
    print(f" Watching: {VAULT_PUBLISH_DIR}")
    print("=" * 60)

    VAULT_PUBLISH_DIR.mkdir(parents=True, exist_ok=True)
    PUBLISHED_DIR.mkdir(parents=True, exist_ok=True)

    handler  = MarkdownHandler()
    observer = Observer()
    observer.schedule(handler, str(VAULT_PUBLISH_DIR), recursive=False)
    observer.start()

    try:
        while True:
            time.sleep(1)
            handler.flush_pending()
    except KeyboardInterrupt:
        log_info("Shutting down…")
    finally:
        observer.stop()
        observer.join()


if __name__ == "__main__":
    main()

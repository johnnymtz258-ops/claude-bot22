"""Make FomoBot_review.zip (a small copy of the bot's data for review) and show it in Finder.

Saved in this bot folder AND on your Desktop. Nothing from .env goes in it.
You can also type /export in Telegram and the bot sends you the same file.
"""
import shutil
import subprocess
import sys
from pathlib import Path

from fomo import config
from fomo.export import build_review_zip

ROOT = Path(__file__).resolve().parent


def main() -> int:
    try:
        zipped = build_review_zip(config.load().db_path, ROOT / "FomoBot_review.zip")
    except Exception as exc:
        print(f"\n❌ Export failed: {type(exc).__name__}: {exc}\nSend a screenshot of this window.")
        return 1
    places = [zipped]
    desktop = Path.home() / "Desktop"
    try:
        shutil.copy2(zipped, desktop / zipped.name)
        places.append(desktop / zipped.name)
    except OSError:
        pass  # no Desktop access for Terminal: the copy in the bot folder is enough
    print(f"\n✅ Done ({zipped.stat().st_size / 1e6:.1f} MB). Send this file:")
    for p in places:
        print(f"   {p}")
    if sys.platform == "darwin":
        subprocess.run(["open", "-R", str(zipped)], check=False)  # opens Finder with the file selected
        print("A Finder window just opened with the file selected.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

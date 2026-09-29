"""Append settings that are new in this version to .env, without changing existing values."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def keys_in(text: str) -> set:
    return {line.split("=", 1)[0].strip() for line in text.splitlines()
            if "=" in line and not line.lstrip().startswith("#")}


def main() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    existing = keys_in(env.read_text())
    new = [line for line in (ROOT / ".env.example").read_text().splitlines()
           if "=" in line and not line.lstrip().startswith("#") and line.split("=", 1)[0].strip() not in existing]
    if new:
        with env.open("a") as f:
            f.write("\n# --- added by FomoBot Whale Copy (defaults; edit freely) ---\n" + "\n".join(new) + "\n")
        print(f"Added {len(new)} new settings to .env (your existing values were not changed).")


if __name__ == "__main__":
    main()

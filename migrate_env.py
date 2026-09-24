from pathlib import Path
ROOT = Path(__file__).resolve().parent
env = ROOT / ".env"
example = ROOT / ".env.example"
if not env.exists():
    raise SystemExit(0)
existing = env.read_text()
keys = set()
for line in existing.splitlines():
    if "=" in line and not line.lstrip().startswith("#"):
        keys.add(line.split("=", 1)[0].strip())
append = []
for line in example.read_text().splitlines():
    if "=" in line and not line.lstrip().startswith("#"):
        key = line.split("=", 1)[0].strip()
        if key not in keys:
            append.append(line)
if append:
    with env.open("a") as f:
        f.write("\n# --- newer defaults added automatically ---\n")
        f.write("\n".join(append) + "\n")
    print(f"Added {len(append)} new settings to .env without changing your existing keys.")

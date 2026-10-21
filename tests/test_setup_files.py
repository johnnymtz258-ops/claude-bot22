"""Moving to a new version folder: settings come along, nothing gets overwritten."""
import import_secrets as imp


def _bot_folder(parent, name, env_text, kind="bot.py"):
    folder = parent / name
    folder.mkdir()
    (folder / kind).write_text("")
    (folder / ".env").write_text(env_text)
    return folder


def test_a_new_folder_gets_fomobot_settings_from_the_old_one(tmp_path):
    _bot_folder(tmp_path, "FOMO_BOT_31_0", "TELEGRAM_BOT_TOKEN=123:abc\nTELEGRAM_CHAT_ID=42\nSOLANA_RPC_HTTP=https://rpc\n"
                                           "TRADING_PRIVATE_KEY=never\nMIN_WHALE_BUY_USD=500\n")
    new = tmp_path / "FOMO_BOT_31_0_DAYBOT_1_0"
    new.mkdir()
    (new / ".env.example").write_text("TELEGRAM_BOT_TOKEN=\nTELEGRAM_CHAT_ID=\nSOLANA_RPC_HTTP=\n")
    assert imp.main(new, home=tmp_path / "nohome") == 0
    env = imp.read_env(new / ".env")
    assert env == {"TELEGRAM_BOT_TOKEN": "123:abc", "TELEGRAM_CHAT_ID": "42", "SOLANA_RPC_HTTP": "https://rpc"}
    assert "never" not in (new / ".env").read_text() and "MIN_WHALE_BUY_USD" not in (new / ".env").read_text()


def test_daybot_keys_already_entered_are_kept_and_fomobot_settings_added(tmp_path):
    _bot_folder(tmp_path, "FOMO_BOT_31_0", "TELEGRAM_BOT_TOKEN=123:abc\nSOLANA_RPC_HTTP=https://rpc\n")
    new = _bot_folder(tmp_path, "FOMO_BOT_31_0_DAYBOT_1_0", "\n# --- DayBot ---\nALPACA_API_KEY_ID=AK1\n"
                                                             "DAY_TELEGRAM_BOT_TOKEN=999:day\n", kind="day.py")
    assert imp.main(new, home=tmp_path / "nohome") == 0
    env = imp.read_env(new / ".env")
    assert env["ALPACA_API_KEY_ID"] == "AK1" and env["DAY_TELEGRAM_BOT_TOKEN"] == "999:day"
    assert env["TELEGRAM_BOT_TOKEN"] == "123:abc" and env["SOLANA_RPC_HTTP"] == "https://rpc"
    assert imp.main(new, home=tmp_path / "nohome") == 0                   # running again changes nothing
    assert imp.read_env(new / ".env") == env


def test_daybot_keys_move_to_the_next_version_and_nothing_here_is_overwritten(tmp_path):
    _bot_folder(tmp_path, "OLD", "TELEGRAM_BOT_TOKEN=1:a\nALPACA_API_KEY_ID=AK_OLD\nALPACA_API_SECRET_KEY=S\n"
                                 "SNAPTRADE_CLIENT_ID=C\nSNAPTRADE_CONSUMER_KEY=K\n")
    new = _bot_folder(tmp_path, "NEW", "TELEGRAM_BOT_TOKEN=2:b\n", kind="day.py")
    imp.main(new, home=tmp_path / "nohome")
    env = imp.read_env(new / ".env")
    assert env["TELEGRAM_BOT_TOKEN"] == "2:b"                              # this folder's own value wins
    assert env["ALPACA_API_KEY_ID"] == "AK_OLD" and env["SNAPTRADE_CONSUMER_KEY"] == "K"


def test_unrelated_folders_and_nothing_found(tmp_path, capsys):
    other = tmp_path / "some_project"
    other.mkdir()
    (other / ".env").write_text("TELEGRAM_BOT_TOKEN=not-a-bot-folder\n")
    new = tmp_path / "NEW"
    new.mkdir()
    assert imp.main(new, home=tmp_path / "nohome") == 2
    assert not (new / ".env").exists() and "No earlier bot folder" in capsys.readouterr().out


def test_bot_folders_in_downloads_are_found_too(tmp_path):
    home = tmp_path / "home"
    (home / "Downloads").mkdir(parents=True)
    _bot_folder(home / "Downloads", "FOMO_BOT_30_0", "TELEGRAM_BOT_TOKEN=7:x\n")
    elsewhere = tmp_path / "Desktop2"
    elsewhere.mkdir()
    new = elsewhere / "NEW"
    new.mkdir()
    assert imp.main(new, home=home) == 0 and imp.read_env(new / ".env")["TELEGRAM_BOT_TOKEN"] == "7:x"


def test_set_key_updates_in_place():
    lines = ["# c", "TELEGRAM_BOT_TOKEN=", "X=1"]
    out = imp.set_key(lines, "TELEGRAM_BOT_TOKEN", "t")
    assert out == ["# c", "TELEGRAM_BOT_TOKEN=t", "X=1"]
    out = imp.set_key(out, "ALPACA_API_KEY_ID", "a")
    assert out[-2:] == ["# --- DayBot ---", "ALPACA_API_KEY_ID=a"]


def test_a_key_added_later_in_any_folder_is_found(tmp_path):
    import os
    import time as _t
    old = _bot_folder(tmp_path, "DayBot", "ALPACA_API_KEY_ID=AK\nSNAPTRADE_CLIENT_ID=PERS-1\nSNAPTRADE_CONSUMER_KEY=CK-added\n",
                      kind="day.py")
    newer = _bot_folder(tmp_path, "DAYBOT_1_0", "ALPACA_API_KEY_ID=AK\nSNAPTRADE_CLIENT_ID=PERS-1\n", kind="day.py")
    os.utime(old / ".env", (_t.time() - 3600, _t.time() - 3600))       # the folder with the key is the older one
    new = _bot_folder(tmp_path, "DayBot_final", "ALPACA_API_KEY_ID=AK\nSNAPTRADE_CLIENT_ID=PERS-1\nSNAPTRADE_CONSUMER_KEY=\n",
                      kind="day.py")
    imp.main(new, home=tmp_path / "nohome")
    assert imp.read_env(new / ".env")["SNAPTRADE_CONSUMER_KEY"] == "CK-added"
    assert newer.exists()


def test_screenshot_type_is_read_from_the_file():
    from daybot.ai import sniff_image
    assert sniff_image(b"\x89PNG\r\n\x1a\n....") == "image/png" and sniff_image(b"\xff\xd8\xff\xe0") == "image/jpeg"
    assert sniff_image(b"RIFF1234WEBPVP8 ") == "image/webp" and sniff_image(b"hello") == ""

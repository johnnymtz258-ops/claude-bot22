import sqlite3

from fomo.commands import _amount_and_mc
from tests.demo_app import COINS, WHALES

CHAT = "12345"


class FakeTelegram:
    ok = True
    chat_id = CHAT

    def __init__(self):
        self.out, self.answers, self.edits = [], [], []
        self._id = 500

    async def send(self, text, *, buttons=None, silent=False, reply_to=None, chat_id=None):
        self._id += 1
        self.out.append({"id": self._id, "text": text, "buttons": buttons, "chat": chat_id or CHAT})
        return self._id

    async def edit(self, message_id, text, buttons=None):
        self.edits.append(text)
        return message_id in {m["id"] for m in self.out}

    async def pin(self, message_id):
        self.pinned = message_id
        return True

    async def answer(self, callback_id, text=""):
        self.answers.append(text)


def test_every_read_command_answers(chat):
    for cmd in ("/help", "/whales", "/recent", "/hot", "/positions", "/stats", "/stats 7", "/status", "/settings",
                "/whale Rocket", "/coin CASHED", "/runners", "/suggest", "/scout", "/exits"):
        reply = chat.say(cmd)
        assert reply and "⚠️" not in reply.split("\n")[0], (cmd, reply)
    assert "Rocket" in chat.say("/whales") and "HOT" in chat.say("/whales")
    assert "Dino" in chat.say(f"/coin {COINS['CASHED'][0]}")


def test_strangers_are_ignored(chat):
    assert chat.say("/whales", chat_id="999") == ""


def test_commands_sent_before_startup_are_not_replayed(chat):
    before = len(chat.telegram.out)
    chat.loop.run_until_complete(chat.commands.handle_update(
        {"message": {"chat": {"id": int(CHAT)}, "text": "/bought 20 MOIN", "date": int(chat.started) - 3600}}))
    assert len(chat.telegram.out) == before


def test_bought_by_replying_to_an_alert_at_a_market_cap(chat):
    alert_id = chat.db.scalar("select message_id from tg_messages where mint=? limit 1", (COINS["BAGSPAY"][0],))
    if not alert_id:  # the demo notifier has no Telegram, so map a fake alert message to the coin
        chat.db.run("insert into tg_messages(message_id,mint,wallet,kind,ts) values(77,?,?,?,0)",
                    (COINS["BAGSPAY"][0], WHALES["Cupsey"], "BUY"))
        alert_id = 77
    reply = chat.say("/bought 20 at 500k", reply_to=alert_id)
    assert "Bought $20.00 of $BAGSPAY at $500K MC" in reply
    reply = chat.say("/sold 50%", reply_to=alert_id)
    assert "Sold" in reply and "still holding" in reply
    assert "Removed your last entry" in chat.say("/undo")


def test_sold_by_ticker_you_hold(chat):
    assert "Sold" in chat.say("/sold 10 MOIN")


def test_add_in_any_order_and_mute_buttons(chat):
    addr = "EdmxWPmx2WH6WgFfTdu9xfkYf3k1g5wD1zccTVySEEh1"
    assert "Now following Newbie" in chat.say(f"/add Newbie {addr}")
    assert chat.whales.get(addr)["name"] == "Newbie"
    assert "Muted" in chat.press(f"mute:{addr}")
    assert chat.whales.get(addr)["muted"] == 1
    chat.press(f"unmute:{addr}")
    assert chat.whales.get(addr)["muted"] == 0
    assert "Removed" in chat.say("/remove Newbie")


def test_set_and_pause(chat):
    assert "MIN_WHALE_BUY_USD = 250" in chat.say("/set MIN_WHALE_BUY_USD 250")
    assert "must be between" in chat.say("/set MIN_WHALE_BUY_USD -3")
    assert "paused" in chat.say("/pause") and not chat.cfg.flag("ALERTS_ENABLED")
    chat.say("/resume")
    assert chat.cfg.flag("ALERTS_ENABLED")


def test_pasting_an_address_shows_the_coin(chat):
    assert "Tracked whales" in chat.say(COINS["CASHED"][0])


def test_bad_input_is_explained_not_crashing(chat):
    assert "which coin" in chat.say("/bought 20")
    assert "no whale by that name" in chat.say("/whale nobody")
    assert "Unknown command" in chat.say("/wat")


def test_amount_parsing():
    assert _amount_and_mc(["20", "at", "850k"]) == (20, 850_000)
    assert _amount_and_mc(["$1,200", "@1.2m"]) == (1200, 1_200_000)
    assert _amount_and_mc(["30%"]) == (0, 0)


def test_export_command_sends_the_file(chat, tmp_path):
    path = tmp_path / "whales.db"
    disk = sqlite3.connect(str(path))
    chat.db.conn.backup(disk)
    disk.close()
    chat.cfg.db_path = path
    sent = []

    async def send_document(p, caption="", chat_id=None):
        sent.append((p.name, p.stat().st_size, caption))
        return True

    chat.telegram.send_document = send_document
    assert "Packing" in chat.say("/export")
    assert sent and sent[0][0] == "FomoBot_review.zip" and "no keys" in sent[0][2]


def test_live_card_is_sent_pinned_then_edited(chat):
    from fomo.card import CARD_BUTTONS, LiveCard
    card = LiveCard(chat)
    msg_id = chat.loop.run_until_complete(card.update())
    assert msg_id and chat.telegram.pinned == msg_id
    text = chat.telegram.out[-1]["text"]
    assert "FomoBot" in text and "whales" in text and "🤖 Paper" in text and "You hold" in text
    assert chat.telegram.out[-1]["buttons"] == CARD_BUTTONS
    sent = len(chat.telegram.out)
    chat.loop.run_until_complete(card.update())             # nothing changed: no edit, no new message
    assert len(chat.telegram.out) == sent and chat.telegram.edits == []
    chat.loop.run_until_complete(card.update(force=True))   # forced: edited in place
    assert len(chat.telegram.out) == sent and len(chat.telegram.edits) == 1


def test_card_buttons_run_commands(chat):
    chat.card = None
    chat.press("cmd:positions")
    assert "Your positions" in chat.telegram.out[-1]["text"]
    chat.press("cmd:pause")
    assert chat.cfg.flag("ALERTS_ENABLED") is False
    chat.press("cmd:resume")
    assert chat.cfg.flag("ALERTS_ENABLED") is True


def test_paper_command(chat):
    assert "Paper autopilot" in chat.say("/paper")

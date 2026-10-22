import os
import tempfile
import unittest
from pathlib import Path
import importlib.util
import sys
import types

# Stub Telegram imports: these tests exercise the database/business logic only.
tg = types.ModuleType("telegram")
class DummyUpdate: pass
class DummyBot: pass
tg.Update = DummyUpdate
tg.ParseMode = types.SimpleNamespace(HTML="HTML")
constants = types.ModuleType("telegram.constants")
constants.ParseMode = types.SimpleNamespace(HTML="HTML")
ext = types.ModuleType("telegram.ext")
class DummyApplication:
    @classmethod
    def builder(cls): return cls()
    def token(self, *a): return self
    def post_init(self, *a): return self
    def build(self): return self
    def add_handler(self, *a): return self
    def run_polling(self, **kw): return None
class DummyMessageHandler:
    def __init__(self,*a,**kw): pass
class DummyFilters:
    TEXT = object()
    COMMAND = object()
    def __and__(self, other): return self
    def __invert__(self): return self
ext.Application = DummyApplication
ext.ContextTypes = types.SimpleNamespace(DEFAULT_TYPE=object)
ext.MessageHandler = DummyMessageHandler
ext.filters = DummyFilters()
sys.modules["telegram"] = tg
sys.modules["telegram.constants"] = constants
sys.modules["telegram.ext"] = ext

# Load bot module with a temporary isolated database.
tmp = tempfile.TemporaryDirectory()
os.environ["DB_PATH"] = str(Path(tmp.name) / "test.sqlite3")
spec = importlib.util.spec_from_file_location("bot", Path(__file__).parents[1] / "bot.py")
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)

class DistributionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        bot.init_db()

    def setUp(self):
        with bot.db_connect() as db:
            db.execute("DELETE FROM registrations")
            db.execute("DELETE FROM distributions")
            db.execute("DELETE FROM group_counters")

    def make_dist(self, source):
        return bot.create_distribution(-100123, source, 99)[0]

    def test_independent_distributions_and_registration(self):
        a = self.make_dist(1001)
        b = self.make_dist(1002)
        s1, r1 = bot.register_member(a["id"], -100123, 1, "Alice", "RDO-A", 2001)
        s2, r2 = bot.register_member(b["id"], -100123, 1, "Alice", "RDO-B", 2002)
        self.assertEqual((s1, s2), ("ok", "ok"))
        self.assertNotEqual(r1["distribution_id"], r2["distribution_id"])
        self.assertNotEqual(r1["registration_number"], r2["registration_number"])
        self.assertEqual(len(bot.list_registrations(a["id"])), 1)
        self.assertEqual(len(bot.list_registrations(b["id"])), 1)

    def test_duplicate_user_cannot_change_game_id(self):
        d = self.make_dist(1003)
        status, first = bot.register_member(d["id"], -100123, 5, "Bob", "FIRST", 2003)
        status2, previous = bot.register_member(d["id"], -100123, 5, "Bob", "SECOND", 2004)
        self.assertEqual(status, "ok")
        self.assertEqual(status2, "duplicate")
        self.assertEqual(previous["game_id"], "FIRST")
        self.assertEqual(previous["registration_number"], first["registration_number"])

    def test_stopped_distribution_rejects_registration(self):
        d = self.make_dist(1004)
        self.assertTrue(bot.stop_distribution(d["id"], -100123))
        status, row = bot.register_member(d["id"], -100123, 7, "Eve", "ID", 2005)
        self.assertEqual(status, "closed")
        self.assertIsNone(row)

    def test_database_persists_after_reopen(self):
        d = self.make_dist(1005)
        bot.register_member(d["id"], -100123, 8, "Dan", "PERSIST", 2006)
        with bot.db_connect() as db:
            found = db.execute(
                "SELECT game_id FROM registrations WHERE distribution_id=?",
                (d["id"],),
            ).fetchone()
        self.assertEqual(found["game_id"], "PERSIST")

if __name__ == "__main__":
    unittest.main()

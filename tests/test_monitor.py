import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cruisewatch import messages
from cruisewatch.config import Config
from cruisewatch.demo import run_demo
from cruisewatch.extract import enrich
from cruisewatch.monitor import Monitor
from cruisewatch.source import FetchError, is_available, parse_feed
from cruisewatch.state import load_state, save_state
from cruisewatch.telegram import Telegram

FIX = Path(__file__).parent / "fixtures"
FEED = json.loads((FIX / "cruise_feed.json").read_text())
DETAILS = json.loads((FIX / "details.json").read_text())


def award(feed, aid):
    for lane in feed["Lanes"]:
        for a in lane["Awards"]:
            if a["AwardID"] == aid:
                return a


def set_award(feed, aid, **kw):
    for lane in feed["Lanes"]:
        for a in lane["Awards"]:
            if a["AwardID"] == aid:
                a.update(kw)


def add_award(feed, a):
    feed["Lanes"][-1]["Awards"].append(copy.deepcopy(a))


def remove_award(feed, aid):
    for lane in feed["Lanes"]:
        lane["Awards"] = [a for a in lane["Awards"] if a["AwardID"] != aid]


class FakeTelegram(Telegram):
    def __init__(self, fail=False):
        super().__init__("t", "c", dry_run=True)
        self.fail = fail

    def send(self, text, *, silent=False):
        if self.fail:
            return False
        return super().send(text, silent=silent)


class Harness:
    def __init__(self, **cfg):
        self.tmp = tempfile.TemporaryDirectory()
        self.feed = copy.deepcopy(FEED)
        self.other = {"Lanes": []}
        self.error = None
        self.now = datetime(2026, 11, 16, 3, 0, tzinfo=timezone.utc)
        self.cfg = Config(state_file=Path(self.tmp.name) / "state.json", daily_summary_hour=-1,
                          sweep_destinations=(1,), dry_run=True, **cfg)
        self.tg = FakeTelegram()

    def fetch(self, url):
        if "/award/" in url:
            d = DETAILS.get(url.rsplit("/", 1)[1])
            if d is None:
                raise FetchError("404")
            return d
        if self.error:
            raise self.error
        if url.endswith("/destination/8"):
            return copy.deepcopy(self.feed)
        return copy.deepcopy(self.other)

    def run(self):
        before = len(self.tg.sent)
        mon = Monitor(self.cfg, telegram=self.tg, fetcher=self.fetch, clock=lambda: self.now)
        result = mon.run()
        self.now += timedelta(minutes=5)
        return result, [m["text"] for m in self.tg.sent[before:]]

    def state(self):
        return load_state(self.cfg.state_file)


NEW = {"AwardID": 5001, "OfferID": 6001, "PartnerName": "Royal Caribbean",
       "Title": "6 Night Alaska", "LocationName": "", "Price": 350000, "Quantity": 5,
       "SnipeText": "NEW REWARD!", "SnipeCategory": "Custom"}


class AvailabilityTests(unittest.TestCase):
    def test_rules(self):
        self.assertTrue(is_available(5, None))
        self.assertFalse(is_available(0, None))
        self.assertFalse(is_available(None, "sold out", "SoldOut"))
        self.assertTrue(is_available(None, None))            # unlimited (e.g. NCL $400 off)
        self.assertTrue(is_available(3, "sold out", "SoldOut"))  # live count beats a stale badge

    def test_parse_dedupes_lanes(self):
        rewards = parse_feed(FEED, 8, cruise_only=False)
        self.assertEqual(len(rewards), 18)  # awards repeat across lanes in the real feed

    def test_bad_shape_is_error(self):
        with self.assertRaises(FetchError):
            parse_feed({"error": "maintenance"}, 8, cruise_only=False)

    def test_sweep_filters_non_cruise(self):
        vegas = {"Lanes": [{"Awards": [
            {"AwardID": 1, "PartnerName": "MGM Resorts International", "Title": "Buffet for 2"},
            {"AwardID": 2, "PartnerName": "Celebrity Cruises", "Title": "$250 off"},
        ]}]}
        self.assertEqual(list(parse_feed(vegas, 1, cruise_only=True)), ["2"])


class DetectionTests(unittest.TestCase):
    def test_first_run_is_baseline_only(self):
        h = Harness()
        _, msgs = h.run()
        self.assertEqual(len(msgs), 1)
        self.assertIn("monitor is live", msgs[0])
        self.assertEqual(len(h.state()["rewards"]), 18)

    def test_unchanged_feed_never_realerts_and_writes_nothing(self):
        h = Harness()
        h.run()
        r1, m1 = h.run()
        r2, m2 = h.run()
        self.assertEqual(m1 + m2, [])
        self.assertFalse(r2["state_changed"])

    def test_new_reward_alerts_once(self):
        h = Harness()
        h.run()
        add_award(h.feed, NEW)
        _, msgs = h.run()
        self.assertEqual(len(msgs), 1)
        self.assertIn("NEW cruise reward", msgs[0])
        self.assertIn("350,000 pts", msgs[0])
        _, again = h.run()
        self.assertEqual(again, [])

    def test_new_reward_already_sold_out_still_alerts(self):
        h = Harness()
        h.run()
        add_award(h.feed, dict(NEW, Quantity=0, SnipeText="sold out", SnipeCategory="SoldOut"))
        _, msgs = h.run()
        self.assertEqual(len(msgs), 1)
        self.assertIn("already sold out", msgs[0])

    def test_restock_alerts_once_and_sold_out_is_silent(self):
        h = Harness()
        h.run()
        set_award(h.feed, 11024, Quantity=2, SnipeText=None, SnipeCategory=None)
        _, msgs = h.run()
        self.assertEqual(len(msgs), 1)
        self.assertIn("BACK IN STOCK", msgs[0])
        self.assertIn("Spectrum of the Seas", msgs[0])
        self.assertIn("Hong Kong", msgs[0])
        self.assertIn("11/16/2026", msgs[0])
        self.assertEqual(h.run()[1], [])
        set_award(h.feed, 11024, Quantity=0, SnipeText="sold out", SnipeCategory="SoldOut")
        self.assertEqual(h.run()[1], [])  # sold out: silent by default
        set_award(h.feed, 11024, Quantity=1, SnipeText=None, SnipeCategory=None)
        _, msgs = h.run()
        self.assertEqual(len(msgs), 1)    # second restock is a new transition: alert again
        self.assertIn("BACK IN STOCK", msgs[0])

    def test_quantity_increase(self):
        h = Harness()
        h.run()
        set_award(h.feed, 6713, Quantity=19 + 10)
        _, msgs = h.run()
        self.assertEqual(len(msgs), 1)
        self.assertIn("More inventory", msgs[0])
        set_award(h.feed, 6713, Quantity=20)  # normal selling down
        self.assertEqual(h.run()[1], [])

    def test_single_check_blip_does_not_cause_false_alerts(self):
        h = Harness()
        h.run()
        remove_award(h.feed, 11237)
        self.assertEqual(h.run()[1], [])
        h.feed = copy.deepcopy(FEED)
        self.assertEqual(h.run()[1], [])
        self.assertEqual(h.state()["rewards"]["11237"]["status"], "available")

    def test_removed_then_returned(self):
        h = Harness()
        h.run()
        remove_award(h.feed, 11237)
        h.run(); h.run()
        self.assertEqual(h.state()["rewards"]["11237"]["status"], "removed")
        h.feed = copy.deepcopy(FEED)
        _, msgs = h.run()
        self.assertEqual(len(msgs), 1)
        self.assertIn("BACK in the store", msgs[0])

    def test_removed_sold_out_reward_returning_available_is_restock(self):
        h = Harness()
        h.run()
        remove_award(h.feed, 11020)
        h.run(); h.run()
        h.feed = copy.deepcopy(FEED)
        set_award(h.feed, 11020, Quantity=4, SnipeText=None, SnipeCategory=None)
        _, msgs = h.run()
        self.assertEqual(len(msgs), 1)

    def test_failed_send_is_retried_not_lost(self):
        h = Harness()
        h.run()
        add_award(h.feed, NEW)
        h.tg.fail = True
        h.run()
        self.assertNotIn("5001", h.state()["rewards"])
        h.tg.fail = False
        _, msgs = h.run()
        self.assertEqual(len(msgs), 1)
        self.assertIn("NEW cruise reward", msgs[0])
        self.assertEqual(h.run()[1], [])

    def test_empty_feed_does_not_mark_everything_removed(self):
        h = Harness()
        h.run()
        h.feed = {"Lanes": []}
        msgs = []
        for _ in range(4):
            msgs += h.run()[1]
        self.assertEqual(sum("zero" in m for m in msgs), 1)
        self.assertTrue(all(r["status"] != "removed" for r in h.state()["rewards"].values()))
        h.feed = copy.deepcopy(FEED)
        _, msgs = h.run()
        self.assertEqual(len(msgs), 1)  # just the "showing again" note; no restock flood
        self.assertIn("showing again", msgs[0])

    def test_outage_alert_once_then_recovery(self):
        h = Harness()
        h.run()
        h.error = FetchError("503")
        msgs = []
        for _ in range(6):
            r, m = h.run()
            self.assertFalse(r["ok"])
            msgs += m
        self.assertEqual(len(msgs), 1)
        self.assertIn("monitor problem", msgs[0])
        h.error = None
        _, m = h.run()
        self.assertIn("recovered", m[0])

    def test_sweep_destination_baselined_silently_then_new_alerts(self):
        h = Harness()
        h.other = {"Lanes": [{"Awards": [{"AwardID": 777, "OfferID": 1, "PartnerName": "Virgin Voyages",
                                          "Title": "Sailing credit", "Price": 9000, "Quantity": 3}]}]}
        _, msgs = h.run()
        self.assertEqual(len(msgs), 1)  # only the startup message
        self.assertIn("777", h.state()["rewards"])
        h.now += timedelta(minutes=61)
        h.other["Lanes"][0]["Awards"].append({"AwardID": 778, "OfferID": 2, "PartnerName": "Virgin Voyages",
                                              "Title": "Cruise for 2", "Price": 9000, "Quantity": 3})
        _, msgs = h.run()
        self.assertEqual(len(msgs), 1)
        self.assertIn("Virgin Voyages", msgs[0])

    def test_unswept_destination_is_not_counted_missing(self):
        h = Harness()
        h.other = {"Lanes": [{"Awards": [{"AwardID": 777, "OfferID": 1, "PartnerName": "Virgin Voyages",
                                          "Title": "Sailing credit", "Price": 9000, "Quantity": 3}]}]}
        h.run()
        for _ in range(5):  # within the sweep interval, destination 1 isn't fetched
            h.run()
        self.assertEqual(h.state()["rewards"]["777"]["status"], "available")

    def test_mass_change_sends_digest(self):
        h = Harness(max_individual_alerts=3)
        h.run()
        for i in range(6):
            add_award(h.feed, dict(NEW, AwardID=9000 + i, OfferID=9100 + i, Title=f"Cruise {i}"))
        _, msgs = h.run()
        self.assertIn("6 cruise reward changes", msgs[0])
        self.assertEqual(len(msgs), 1 + 3)


class ExtractionTests(unittest.TestCase):
    def test_real_ncl_reward(self):
        rec = {"title": "Comp 7 Day Mexican Cruise", "location": "Port: San Diego, California"}
        info = enrich(rec, DETAILS["41230"])
        self.assertEqual(info["ship"], "Norwegian Star")
        self.assertIn("San Diego", info["port"])
        self.assertEqual(info["sailing"], "January 2 – February 6, 2027")
        self.assertEqual(info["book_by"], "November 30, 2026")

    def test_real_rccl_reward(self):
        info = enrich({"title": "4 Night Chan May Cruise", "location": ""}, DETAILS["40961"])
        self.assertEqual(info["ship"], "Spectrum of the Seas")
        self.assertEqual(info["port"], "Hong Kong, China")
        self.assertEqual(info["sailing"], "11/16/2026")
        self.assertEqual(info["nights"], "4 nights")

    def test_exclusion_is_not_reported_as_ship(self):
        detail = {"Description": "Save on your next cruise.",
                  "Popouts": [{"Title": "Terms and Conditions",
                               "Body": "Valid for sailings departing by November 30, 2027. "
                                       "Not valid on Pride of America® sailings."}]}
        info = enrich({"title": "Enjoy $400 off at NCL", "location": "Various Ports"}, detail)
        self.assertEqual(info["ship"], "Any ship (excluding Pride of America)")
        self.assertEqual(info["port"], "Various ports")

    def test_missing_detail_is_graceful(self):
        info = enrich({"title": "$25 in Free Play", "location": "Any Sailing"}, None)
        self.assertIsNone(info["ship"])
        self.assertIsNone(info["port"])


class MessageAndStateTests(unittest.TestCase):
    def test_html_escaped(self):
        rec = {"partner": "A&B <Cruises>", "title": "x < y", "status": "available", "quantity": 1, "price": 5}
        text = "\n".join(messages.reward_lines(rec))
        self.assertIn("A&amp;B &lt;Cruises&gt;", text)

    def test_state_roundtrip_and_no_rewrite(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.json"
            st = load_state(p)
            st["rewards"]["1"] = {"title": "t"}
            self.assertTrue(save_state(p, st))
            self.assertFalse(save_state(p, load_state(p)))

    def test_corrupt_state_refuses_to_reset(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.json"
            p.write_text("{not json")
            with self.assertRaises(RuntimeError):
                load_state(p)

    def test_demo_scenario(self):
        results = dict(run_demo(out=lambda *_: None))
        counts = {label.split(":")[0]: len(m) for label, m in results.items()}
        self.assertEqual(counts, {
            "Check 1": 1, "Check 2": 0, "Check 3": 1, "Check 4": 0, "Check 5": 1, "Check 6": 0,
            "Check 7": 0, "Check 8": 0, "Check 9": 0, "Check 10": 0, "Check 11": 1, "Check 12": 1,
            "Check 13": 1, "Check 14": 1,
        })


if __name__ == "__main__":
    unittest.main()

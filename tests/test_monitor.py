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
        self.now = datetime(2026, 10, 20, 3, 0, tzinfo=timezone.utc)
        self.cfg = Config(state_file=Path(self.tmp.name) / "state.json",
                          sweep_destinations=(1,), dry_run=True,
                          **{"only_cruise_line": "", "only_full_cruises": False,
                             "only_departure_region": "any", "first_run_summary": True,
                             "notify_problems": True, "daily_summary_hour": -1, **cfg})
        self.tg = FakeTelegram()

    def fetch(self, url):
        if "/award/" in url:
            offer = url.rsplit("/", 1)[1]
            live = next((a for lane in self.feed.get("Lanes", []) + self.other.get("Lanes", [])
                         for a in lane["Awards"] if str(a.get("OfferID")) == offer), None)
            d = DETAILS.get(offer)
            if d is None and live is None:
                raise FetchError("404")
            # Like the real API, the page carries the live quantity/badge
            # (a test can override them by setting them on DETAILS).
            page = {k: live.get(k) for k in ("Quantity", "SnipeText", "SnipeCategory", "ExpireTime")} if live else {}
            page.update(d or {})
            return page
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

    def test_new_reward_already_sold_out_is_not_sent_but_later_restock_is(self):
        h = Harness()
        h.run()
        add_award(h.feed, dict(NEW, Quantity=0, SnipeText="sold out", SnipeCategory="SoldOut"))
        self.assertEqual(h.run()[1], [])
        set_award(h.feed, 5001, Quantity=2, SnipeText=None, SnipeCategory=None)
        _, msgs = h.run()
        self.assertEqual(len(msgs), 1)
        self.assertIn("BACK IN STOCK", msgs[0])

    def test_restock_after_sailing_date_is_not_sent(self):
        """The 17 Nov 2026 false alert: Chan May (sails 11/16/2026) showed 1 left after it sailed."""
        h = Harness()
        h.now = datetime(2026, 11, 17, 3, 15, tzinfo=timezone.utc)
        h.run()
        set_award(h.feed, 11024, Quantity=1, SnipeText="1 left", SnipeCategory=None)
        self.assertEqual(h.run()[1], [])

    def test_restock_too_close_to_sailing_is_not_sent(self):
        h = Harness()
        h.now = datetime(2026, 11, 14, 3, 0, tzinfo=timezone.utc)   # 2 days before 11/16 sailing
        h.run()
        set_award(h.feed, 11024, Quantity=1, SnipeText=None, SnipeCategory=None)
        self.assertEqual(h.run()[1], [])

    def test_page_without_quantity_is_not_sent(self):
        h = Harness()
        h.run()
        add_award(h.feed, NEW)
        real_fetch = h.fetch
        h.fetch = lambda url: ({k: v for k, v in real_fetch(url).items() if k != "Quantity"}
                               if "/award/" in url else real_fetch(url))
        self.assertEqual(h.run()[1], [])

    def test_page_unreachable_is_not_sent(self):
        h = Harness()
        h.run()
        add_award(h.feed, NEW)
        real_fetch = h.fetch
        def fetch(url):
            if "/award/" in url:
                raise FetchError("timeout")
            return real_fetch(url)
        h.fetch = fetch
        self.assertEqual(h.run()[1], [])

    def test_live_page_overrides_stale_feed(self):
        h = Harness()
        h.run()
        set_award(h.feed, 11024, Quantity=1, SnipeText=None, SnipeCategory=None)
        DETAILS["40961"]["Quantity"], DETAILS["40961"]["SnipeCategory"] = 0, "SoldOut"
        try:
            self.assertEqual(h.run()[1], [])          # page says sold out: no alert
            self.assertEqual(h.state()["rewards"]["11024"]["status"], "sold_out")
        finally:
            DETAILS["40961"].pop("Quantity"); DETAILS["40961"].pop("SnipeCategory")
        _, msgs = h.run()                              # page now agrees it's available
        self.assertEqual(len(msgs), 1)

    def test_expired_reward_is_not_sent(self):
        h = Harness()
        h.run()
        add_award(h.feed, dict(NEW, ExpireTime="2026-10-01T00:00:00+00:00"))
        self.assertEqual(h.run()[1], [])

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


RC_ASIA = {"AwardID": 7001, "OfferID": 7101, "PartnerName": "Royal Caribbean",
           "Title": "6 Night Taiwan & Japan", "LocationName": "", "Price": 380000, "Quantity": 5,
           "ShortDescription": "Enjoy a sailing for 2 on Royal Caribbeans Spectrum of the Seas out of "
                               "Hong Kong, China for 6 nights sailing on 02/02/2027 in an interior cabin."}
RC_USA = dict(RC_ASIA, AwardID=7002, OfferID=7102, Title="7 Night Western Caribbean",
              ShortDescription="Enjoy a sailing for 2 on Royal Caribbeans Wonder of the Seas out of "
                               "Port Canaveral, Florida for 7 nights sailing on 02/14/2027.")


class QuietDefaultsTests(unittest.TestCase):
    """Real defaults: the ONLY messages are 'you can buy this now' alerts."""

    def test_only_purchasable_alerts_are_sent(self):
        h = Harness(**{f: getattr(Config(), f) for f in (
            "first_run_summary", "notify_problems", "daily_summary_hour",
            "only_cruise_line", "only_full_cruises", "only_departure_region")})
        self.assertEqual(h.run()[1], [])                         # no startup list
        h.error = FetchError("503")
        for _ in range(5):
            self.assertEqual(h.run()[1], [])                     # no outage notice
        h.error = None
        self.assertEqual(h.run()[1], [])                         # no recovery notice
        h.feed = {"Lanes": []}
        for _ in range(4):
            self.assertEqual(h.run()[1], [])                     # no empty-store warning
        h.feed = copy.deepcopy(FEED)
        self.assertEqual(h.run()[1], [])
        h.now = datetime(2026, 11, 20, 3, 0, tzinfo=timezone.utc)  # past 09:00 local: still no check-in
        self.assertEqual(h.run()[1], [])
        set_award(h.feed, 11021, Quantity=2, SnipeText=None, SnipeCategory=None)  # Best of Asia restock
        _, msgs = h.run()
        self.assertEqual(len(msgs), 1)
        self.assertIn("BACK IN STOCK", msgs[0])


class FilterTests(unittest.TestCase):
    """Default filter: Royal Caribbean, real 'N Night' cruises, departing Asia."""

    def harness(self):
        return Harness(only_cruise_line="Royal Caribbean", only_full_cruises=True, only_departure_region="asia")


    def test_only_rc_asia_cruises_alert(self):
        h = self.harness()
        _, msgs = h.run()
        self.assertIn("5</b> matching", msgs[0])          # the 5 sold-out Spectrum of the Seas cruises
        self.assertNotIn("Free Play", msgs[0])
        self.assertNotIn("NCL", msgs[0])
        add_award(h.feed, RC_ASIA)
        add_award(h.feed, RC_USA)
        add_award(h.feed, dict(NEW, AwardID=7003, OfferID=7103, PartnerName="Norwegian Cruise Line",
                               Title="Comp 5 Night Cruise"))
        add_award(h.feed, dict(NEW, AwardID=7004, OfferID=7104, Title="$500 in FREEPLAY"))
        _, msgs = h.run()
        self.assertEqual(len(msgs), 1)
        self.assertIn("6 Night Taiwan &amp; Japan", msgs[0])
        self.assertEqual(h.run()[1], [])                   # filtered ones never resurface

    def test_rc_asia_restock_alerts_but_ncl_restock_does_not(self):
        h = self.harness()
        h.run()
        set_award(h.feed, 11021, Quantity=2, SnipeText=None, SnipeCategory=None)   # 8 Night Best of Asia
        set_award(h.feed, 6713, Quantity=40)                                        # RCCL freeplay top-up
        _, msgs = h.run()
        self.assertEqual(len(msgs), 1)
        self.assertIn("Best of Asia", msgs[0])

    def test_unknown_port_still_alerts(self):
        h = self.harness()
        h.run()
        add_award(h.feed, dict(RC_ASIA, AwardID=7005, OfferID=7105, Title="5 Night Mystery Cruise",
                               ShortDescription="A great cruise."))
        _, msgs = h.run()
        self.assertEqual(len(msgs), 1)

    def test_region_list(self):
        from cruisewatch.filters import region_matches
        for port in ("Hong Kong, China", "Singapore", "Shanghai (Baoshan), China", "Tokyo (Yokohama), Japan",
                     "Keelung, Taiwan", "Busan, South Korea", "Laem Chabang (Bangkok), Thailand"):
            self.assertTrue(region_matches(port, "asia"), port)
        for port in ("Port Canaveral, Florida", "Miami, FL", "Barcelona, Spain", "Sydney, Australia"):
            self.assertFalse(region_matches(port, "asia"), port)


class DailyMessageTests(unittest.TestCase):
    def test_pulled_rewards_are_explained(self):
        now = datetime(2026, 11, 19, 2, 38, tzinfo=timezone.utc)
        recs = [
            {"title": "8 Night Best of Asia", "partner": "Royal Caribbean", "status": "sold_out", "price": 450000},
            {"title": "9 Night Best of Japan", "partner": "Royal Caribbean", "status": "removed", "price": 500000,
             "removed_at": "2026-11-19T01:49:24+00:00"},
            {"title": "Old pulled cruise", "partner": "Royal Caribbean", "status": "removed", "price": 1,
             "removed_at": "2026-10-01T00:00:00+00:00"},
        ]
        text = messages.daily_message(recs, now, "Asia/Bangkok", [])
        self.assertIn("1 matching reward in the store", text)
        self.assertIn("Pulled from the store", text)
        self.assertIn("9 Night Best of Japan", text)
        self.assertNotIn("Old pulled cruise", text)


class DateTests(unittest.TestCase):
    def test_parse(self):
        from datetime import date
        from cruisewatch.filters import parse_dates
        self.assertEqual(parse_dates("11/16/2026"), [date(2026, 11, 16)])
        self.assertEqual(max(parse_dates("January 2 – February 6, 2027")), date(2027, 2, 6))
        self.assertEqual(min(parse_dates("January 2 – February 6, 2027")), date(2027, 1, 2))
        self.assertEqual(parse_dates("Sailings departing by November 30, 2027"), [date(2027, 11, 30)])
        self.assertEqual(parse_dates(None), [])


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
            "Check 13": 1, "Check 14": 0, "Check 15": 0,
        })


if __name__ == "__main__":
    unittest.main()

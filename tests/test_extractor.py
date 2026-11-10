import unittest

from crackshot.web.extractor import _draft_fields


class ExtractionTests(unittest.TestCase):
    def test_reserved_card_overrides_other_visible_options(self):
        draft = _draft_fields({
            "city": "温州市",
            "status_text": "已预约",
            "sessions": ["2026-11-13 周五 19:00", "2026-11-15 周日 19:00"],
            "price_tiers": ["318", "618", "818"],
            "reserved_sessions": [{"session": "2026-11-15 周日 19:00",
                                   "on_sale_text": "11月11日 17：00开抢"}],
            "reserved_price_tiers": ["看台￥318.00元"],
        })
        self.assertEqual(draft["city"], "温州")
        self.assertEqual(draft["session_guess"], "2026-11-15 19:00")
        self.assertEqual(draft["price_guess"], "318")
        self.assertEqual(draft["on_sale_guess"], "2026-11-11 17:00")

    def test_reservation_does_not_guess_from_unmarked_or_multiple_options(self):
        for selected in ([], ["318", "618"]):
            with self.subTest(selected=selected):
                draft = _draft_fields({
                    "status_text": "已预约",
                    "sessions": ["2026-11-15 周日 19:00"],
                    "price_tiers": ["318"],
                    "on_sale_text": "2026-11-11 17:00开抢",
                    "reserved_price_tiers": selected,
                })
                for field in ("session_guess", "price_guess", "on_sale_guess"):
                    self.assertEqual(draft[field], "")

    def test_partial_sale_date_uses_previous_year_if_needed(self):
        draft = _draft_fields({
            "sessions": ["2027-01-02 周六 19:00"],
            "price_tiers": ["￥1,118.00"],
            "on_sale_text": "12月30日 18:05开抢",
        })
        self.assertEqual(draft["on_sale_guess"], "2026-12-30 18:05")
        self.assertEqual(draft["price_guess"], "1118")

    def test_invalid_date_and_price_are_left_empty(self):
        draft = _draft_fields({
            "sessions": ["2026-11-31"],
            "price_tiers": ["318元×2张"],
            "on_sale_text": "2026-11-01 27:05开抢",
        })
        for field in ("session_guess", "price_guess", "on_sale_guess"):
            self.assertEqual(draft[field], "")

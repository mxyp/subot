import json
import unittest
from unittest.mock import patch

from subot_core import wallapop


def api_body(items):
    """Build a minimal valid Wallapop API response body."""
    return json.dumps({"data": {"section": {"payload": {"items": items}}}})


def raw_wallapop_item(item_id, price=95.99, subject="Lenovo ThinkCentre M900 Tiny",
                      **overrides):
    """Build a raw Wallapop item mirroring the public API shape."""
    item = {
        "id": item_id,
        "title": subject,
        "price": {"amount": price, "currency": "EUR"},
        "web_slug": f"{subject.lower().replace(' ', '-')}-1326507737",
        "location": {"city": "Tarragona", "region2": "Tarragona"},
    }
    item.update(overrides)
    return item


class SearchParamsTests(unittest.TestCase):
    def test_builds_api_params_from_a_web_search_url(self):
        self.assertEqual(
            wallapop._search_params(
                "https://it.wallapop.com/search?keywords=thinkcentre"
                "&order_by=newest"
            ),
            {
                "source": "side_bar_filters",
                "keywords": "thinkcentre",
                "order_by": "newest",
            },
        )

    def test_requires_a_wallapop_search_url(self):
        for url in (
            "https://example.test/search?keywords=phone",
            "https://it.wallapop.com/item/pc-lenovo-123",
            "not-a-url",
        ):
            with self.subTest(url=url):
                with self.assertRaisesRegex(
                    ValueError, "not a supported Wallapop search URL"
                ):
                    wallapop._search_params(url)

    def test_requires_keywords(self):
        for url in (
            "https://it.wallapop.com/search",
            "https://it.wallapop.com/search?keywords=",
        ):
            with self.subTest(url=url):
                with self.assertRaisesRegex(
                    ValueError, "must include a keywords parameter"
                ):
                    wallapop._search_params(url)

    def test_drops_unknown_order_by_values(self):
        params = wallapop._search_params(
            "https://it.wallapop.com/search"
            "?keywords=thinkcentre&order_by=sponsored"
        )
        self.assertNotIn("order_by", params)

    def test_maps_price_parameters_when_present_and_omits_otherwise(self):
        priced = wallapop._search_params(
            "https://it.wallapop.com/search"
            "?keywords=thinkcentre&priceMin=50&priceMax=19,99"
        )
        self.assertEqual(priced["min_sale_price"], 50.0)
        self.assertEqual(priced["max_sale_price"], 19.99)

        unpriced = wallapop._search_params(
            "https://it.wallapop.com/search?keywords=thinkcentre"
        )
        self.assertNotIn("min_sale_price", unpriced)
        self.assertNotIn("max_sale_price", unpriced)

    def test_rejects_non_numeric_price_parameters(self):
        with self.assertRaisesRegex(ValueError, "priceMax parameter"):
            wallapop._search_params(
                "https://it.wallapop.com/search"
                "?keywords=thinkcentre&priceMax=cheap"
            )

    def test_rejects_non_positive_or_non_finite_price_parameters(self):
        for raw in ("nan", "inf", "0", "-5"):
            with self.subTest(raw=raw):
                with self.assertRaisesRegex(
                    ValueError, "not a positive number"
                ):
                    wallapop._search_params(
                        "https://it.wallapop.com/search"
                        f"?keywords=thinkcentre&priceMax={raw}"
                    )


class ExtractItemsTests(unittest.TestCase):
    def test_extracts_and_deduplicates_items(self):
        body = api_body([raw_wallapop_item("aaa111"), raw_wallapop_item("aaa111"), raw_wallapop_item("bbb222")])
        items = wallapop.extract_items(body)
        self.assertEqual([item["id"] for item in items], ["aaa111", "bbb222"])

    def test_skips_non_dict_entries(self):
        body = api_body(["garbage", raw_wallapop_item("aaa111")])
        items = wallapop.extract_items(body)
        self.assertEqual([item["id"] for item in items], ["aaa111"])

    def test_skips_items_with_an_unhashable_id(self):
        body = api_body([{"id": {"bad": 1}}, raw_wallapop_item("aaa111")])
        items = wallapop.extract_items(body)
        self.assertEqual([item["id"] for item in items], ["aaa111"])

    def test_rejects_a_non_json_body(self):
        with self.assertRaisesRegex(ValueError, "not contain valid JSON"):
            wallapop.extract_items("<html>just a moment</html>")

    def test_rejects_a_body_without_search_results(self):
        with self.assertRaisesRegex(ValueError, "not contain search results"):
            wallapop.extract_items(json.dumps({"data": {}}))


class ParseItemTests(unittest.TestCase):
    def test_normalizes_a_raw_item_into_the_shared_payload(self):
        self.assertEqual(
            wallapop.parse_item(raw_wallapop_item("aaa111", 95.99)),
            {
                "id": "wallapop:aaa111",
                "subject": "Lenovo ThinkCentre M900 Tiny",
                "price": 95.99,
                "url": "https://it.wallapop.com/item/"
                "lenovo-thinkcentre-m900-tiny-1326507737",
                "town": "Tarragona",
                "city": "Tarragona",
            },
        )

    def test_returns_none_without_an_id_or_a_usable_slug(self):
        self.assertIsNone(wallapop.parse_item({"title": "no id"}))
        self.assertIsNone(wallapop.parse_item(raw_wallapop_item("aaa111", web_slug="")))
        self.assertIsNone(
            wallapop.parse_item(raw_wallapop_item("aaa111", web_slug="../escape"))
        )

    def test_price_is_none_when_missing_or_not_positive(self):
        for price in (None, {}, {"amount": 0}, {"amount": -5}, {"amount": "x"}):
            with self.subTest(price=price):
                item = wallapop.parse_item(raw_wallapop_item("aaa111", price=price))
                self.assertIsNone(item["price"])

    def test_town_and_city_are_always_present(self):
        item = wallapop.parse_item(raw_wallapop_item("aaa111", location=None))
        self.assertEqual(item["town"], "")
        self.assertEqual(item["city"], "")


class FetchPageTests(unittest.TestCase):
    @patch("subot_core.wallapop.requests.get")
    def test_fetches_the_api_with_the_shared_http_contract(self, get):
        get.return_value.text = api_body([])
        body = wallapop.fetch_page(
            "https://it.wallapop.com/search?keywords=thinkcentre"
            "&order_by=newest&priceMax=150"
        )

        self.assertEqual(body, api_body([]))
        get.assert_called_once_with(
            wallapop._API_URL,
            params={
                "source": "side_bar_filters",
                "keywords": "thinkcentre",
                "order_by": "newest",
                "max_sale_price": 150.0,
            },
            headers=wallapop._HEADERS,
            timeout=30,
            impersonate="chrome",
        )
        get.return_value.raise_for_status.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()

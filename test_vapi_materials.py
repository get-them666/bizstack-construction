import unittest
from types import SimpleNamespace
from unittest.mock import patch

import construction_main as main
import materials_service


class MaterialsCatalogTests(unittest.TestCase):
    def setUp(self):
        self.original_catalog_rows = materials_service._catalog_rows
        materials_service._catalog_rows = []

    def tearDown(self):
        materials_service._catalog_rows = self.original_catalog_rows

    def test_category_only_search_returns_products(self):
        result = materials_service.search_materials(category="cabinets", query="", limit=8)

        self.assertTrue(result["ok"])
        self.assertTrue(result["items"])
        self.assertTrue(all(item["category"] == "cabinets" for item in result["items"]))

    def test_fixture_category_matches_catalog_rows(self):
        result = materials_service.search_materials(category="fixtures", query="", limit=8)

        self.assertTrue(result["ok"])
        self.assertTrue(result["items"])
        self.assertTrue(all(item["category"] == "fixtures" for item in result["items"]))
        self.assertTrue(any(item["brand"] == "MOEN" for item in result["items"]))

    def test_multi_category_formatter_names_each_category(self):
        result = materials_service.search_materials(
            category="cabinets countertops flooring",
            query="",
            limit=12,
        )

        spoken = main._swaig_tool_response_text("search_materials", result)

        self.assertIn("Cabinets option", spoken)
        self.assertIn("Countertops option", spoken)
        self.assertIn("Flooring option", spoken)
        self.assertTrue("item " in spoken or "model " in spoken)

    def test_catalog_plan_tracks_material_categories(self):
        messages = [
            {"role": "system", "content": "The catalog also includes roofing and bathroom fixtures."},
            {"role": "user", "content": "I am planning a kitchen remodel with the cheapest options."},
            {"role": "assistant", "content": "I can check cabinets, marble counters, and flooring."},
            {"role": "user", "content": "Give me the cheapest brands and item numbers."},
        ]

        self.assertEqual(
            main._vapi_catalog_search_plan(messages),
            ["cabinets", "countertops", "flooring"],
        )

    def test_catalog_plan_does_not_capture_cleaning_pricing(self):
        messages = [
            {"role": "user", "content": "I also need a kitchen remodel."},
            {"role": "user", "content": "What is the cheapest cleaning booking?"},
        ]

        self.assertIsNone(main._vapi_catalog_search_plan(messages))

    def test_vapi_forces_search_and_expands_categories(self):
        completions = SimpleNamespace(calls=[])

        def create(**kwargs):
            completions.calls.append(kwargs)
            if len(completions.calls) == 1:
                function = SimpleNamespace(
                    name="search_materials",
                    arguments='{"category":"cabinets","brand":"IKEA","query":"cheapest","store":"Home Depot","limit":1}',
                )
                message = SimpleNamespace(
                    content=None,
                    tool_calls=[SimpleNamespace(id="call_1", function=function)],
                )
            else:
                message = SimpleNamespace(content="Catalog-backed answer", tool_calls=None)
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

        client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )
        tools = [{"type": "function", "function": {"name": "search_materials"}}]
        tool_choice = {"type": "function", "function": {"name": "search_materials"}}

        with patch.object(main, "_voice_tool_dispatch", return_value="Catalog results") as dispatch:
            answer = main._vapi_assistant_text(
                client,
                "gpt-4o-mini",
                [{"role": "user", "content": "Give me item numbers for a kitchen."}],
                tools,
                object(),
                tool_choice=tool_choice,
                material_categories=["cabinets", "countertops", "flooring"],
            )

        self.assertEqual(answer, "Catalog-backed answer")
        self.assertEqual(completions.calls[0]["tool_choice"], tool_choice)
        self.assertNotIn("tool_choice", completions.calls[1])
        args = dispatch.call_args.args[2]
        self.assertEqual(args["category"], "cabinets countertops flooring")
        self.assertEqual(args["query"], "")
        self.assertEqual(args["limit"], 9)
        self.assertNotIn("brand", args)
        self.assertNotIn("store", args)


if __name__ == "__main__":
    unittest.main()

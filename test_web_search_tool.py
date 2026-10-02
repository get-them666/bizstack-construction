"""The Copilot's web_search tool was pointing at a model that no longer exists.

Symptom the owner reported: the Copilot said it "cannot perform a web search due
to technical issues with the search tool," then offered to fall back to LinkedIn
and people-search sites.

Two defects, both verified here against a fake client rather than a live key:

1. **The tool was dead.** It called the Responses API with tool type
   `web_search_preview` on model `gpt-4o-mini-search-preview`. OpenAI shut both
   of those down on 2026-07-23, so every call 404s on the model. The tool now
   uses the `web_search` tool on a normal model, and walks a model list.

2. **Citations were never returned.** It looked for a `.part` attribute on the
   message item's `content`. In the SDK `content` is a *list* of parts and has
   no `.part`, so the loop body never ran and every successful search reported
   `citations: []`. The old path is asserted here specifically because it fails
   open -- it returns an empty list rather than raising.

Also locked in: the business-contacts-only decision. The blocked domains ride
in the tool's `filters`, not in the prompt, so the Copilot cannot be talked
around them mid-conversation.

No network and no openai package -- `openai.OpenAI` is injected as a fake.
"""

import os
import pathlib
import sys
import types
import unittest

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import copilot_ops


# --- A fake Responses API ----------------------------------------------------
class FakeAnnotation:
    def __init__(self, url):
        self.url = url


class FakePart:
    def __init__(self, annotations):
        self.annotations = annotations


class FakeMessage:
    def __init__(self, annotations):
        self.content = [FakePart(annotations)]


class FakeResponse:
    def __init__(self, text="Roofing contractor result.", urls=()):
        self.output_text = text
        self.output = [FakeMessage([FakeAnnotation(u) for u in urls])]


class FakeResponses:
    """Records every call, and raises for models named in `reject`."""

    def __init__(self, response=None, reject=()):
        self.response = response or FakeResponse()
        self.reject = set(reject)
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        if kw.get("model") in self.reject:
            raise RuntimeError(
                f"The model `{kw.get('model')}` does not exist or you do not have access to it."
            )
        return self.response


class FakeOpenAI:
    def __init__(self, responses):
        self.responses = responses

    def __call__(self):
        return types.SimpleNamespace(responses=self.responses)


def install_fake_openai(fake_responses):
    """Swap copilot_ops' lazy `from openai import OpenAI` for a fake client."""
    mod = types.ModuleType("openai")
    mod.OpenAI = FakeOpenAI(fake_responses)
    return mod


class WebSearchToolTests(unittest.TestCase):
    def setUp(self):
        self._real_openai = sys.modules.get("openai")
        for var in ("OPENAI_SEARCH_MODEL", "SEARCH_BLOCKED_DOMAINS"):
            self._env = getattr(self, "_env", {})
            self._env[var] = os.environ.pop(var, None)

    def tearDown(self):
        if self._real_openai is not None:
            sys.modules["openai"] = self._real_openai
        else:
            sys.modules.pop("openai", None)
        for var, val in getattr(self, "_env", {}).items():
            if val is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = val

    def check(self, ok, msg):
        self.assertTrue(ok, msg)

    def _call(self, fake_responses, query="roofing contractor Virginia Beach"):
        sys.modules["openai"] = install_fake_openai(fake_responses)
        return copilot_ops.build_web_search_tools()["web_search"](query=query)

    # --- the actual outage ---------------------------------------------------
    def test_no_default_model_is_a_shut_down_preview_model(self):
        """2026-07-23 killed the *-search-preview models. None may come back."""
        for model in copilot_ops._search_models():
            self.check(
                "search-preview" not in model,
                f"{model!r} is a retired model; search would 404 again",
            )

    def test_search_succeeds_and_reaches_the_api(self):
        fake = FakeResponses(FakeResponse(urls=["https://example.com/a"]))
        got = self._call(fake)
        self.check(got.get("ok") is True, f"search should succeed, got {got!r}")
        self.check(len(fake.calls) == 1, f"expected one call, got {len(fake.calls)}")
        self.check(fake.calls[0]["model"] in copilot_ops._search_models(),
                   f"called an off-list model: {fake.calls[0]['model']!r}")

    def test_tool_type_is_web_search_not_the_retired_preview(self):
        fake = FakeResponses()
        self._call(fake)
        tool = fake.calls[0]["tools"][0]
        self.check(tool["type"] == "web_search",
                   f"tool type must be web_search, got {tool['type']!r}")

    def test_search_is_required_not_optional(self):
        """With tool_choice auto the model may answer from memory, uncited."""
        fake = FakeResponses()
        self._call(fake)
        self.check(fake.calls[0].get("tool_choice") == "required",
                   f"tool_choice must be required, got {fake.calls[0].get('tool_choice')!r}")

    def test_rejected_first_model_falls_through_to_the_next(self):
        first, second, third = copilot_ops._search_models()[:3]
        fake = FakeResponses(FakeResponse(urls=["https://ok.example"]),
                             reject={first, second})
        got = self._call(fake)
        self.check(got.get("ok") is True, f"should recover, got {got!r}")
        self.check([c["model"] for c in fake.calls] == [first, second, third],
                   f"unexpected model order: {[c['model'] for c in fake.calls]}")

    def test_all_models_rejected_reports_an_error_not_a_crash(self):
        fake = FakeResponses(reject=set(copilot_ops._search_models()))
        got = self._call(fake)
        self.check(got.get("ok") is False, f"expected failure, got {got!r}")
        self.check("search" in got.get("error", "").lower(), got.get("error"))

    def test_non_model_failure_surfaces_without_retrying(self):
        """A rate limit or bad request must not burn two more billed calls."""
        class Boom(FakeResponses):
            def create(self, **kw):
                self.calls.append(kw)
                raise RuntimeError("Rate limit reached for gpt-4.1-mini")

        fake = Boom()
        got = self._call(fake)
        self.check(got.get("ok") is False, f"expected failure, got {got!r}")
        self.check(len(fake.calls) == 1,
                   f"should try exactly one model, tried {len(fake.calls)}")

    def test_empty_answer_is_an_error_not_a_blank_success(self):
        fake = FakeResponses(FakeResponse(text="   "))
        got = self._call(fake)
        self.check(got.get("ok") is False, f"blank result should fail, got {got!r}")

    def test_blank_query_never_bills_a_call(self):
        fake = FakeResponses()
        got = self._call(fake, query="   ")
        self.check(got.get("ok") is False, f"blank query should fail, got {got!r}")
        self.check(not fake.calls, "a blank query must not reach the API")

    # --- the citation bug ----------------------------------------------------
    def test_citations_are_extracted_from_the_real_response_shape(self):
        urls = ["https://a.example", "https://b.example", "https://a.example"]
        got = self._call(FakeResponses(FakeResponse(urls=urls)))
        self.check(got.get("citations") == ["https://a.example", "https://b.example"],
                   f"citations wrong or deduped wrong: {got.get('citations')!r}")

    def test_old_content_part_attribute_returns_nothing(self):
        """The old bug, pinned. This shape is why citations always came back []."""
        class Legacy:
            output = [types.SimpleNamespace(content=types.SimpleNamespace(part=[]))]
        self.check(copilot_ops._extract_citations(Legacy()) == [],
                   "legacy shape should yield [] (it silently did, losing all sources)")

    def test_extraction_never_raises_on_a_hostile_response(self):
        for junk in (None, object(), types.SimpleNamespace(output=None),
                     types.SimpleNamespace(output=[object()]),
                     types.SimpleNamespace(output="not-a-list")):
            try:
                self.check(isinstance(copilot_ops._extract_citations(junk), list),
                           f"should return a list for {junk!r}")
            except Exception as e:
                raise AssertionError(f"extraction raised on {junk!r}: {e}")

    # --- business contacts only ---------------------------------------------
    def test_personal_profile_domains_are_blocked_in_the_tool(self):
        fake = FakeResponses()
        self._call(fake)
        filters = fake.calls[0]["tools"][0].get("filters") or {}
        blocked = filters.get("blocked_domains") or []
        for domain in ("linkedin.com", "facebook.com", "whitepages.com",
                       "truepeoplesearch.com", "spokeo.com"):
            self.check(domain in blocked, f"{domain} must be blocked, got {blocked!r}")

    def test_blocklist_is_overridable(self):
        os.environ["SEARCH_BLOCKED_DOMAINS"] = "example.com, foo.test"
        tool = copilot_ops._search_tool()
        self.check(tool["filters"]["blocked_domains"] == ["example.com", "foo.test"],
                   f"override ignored: {tool['filters']['blocked_domains']!r}")

    def test_empty_override_blocks_nothing_rather_than_everything(self):
        os.environ["SEARCH_BLOCKED_DOMAINS"] = ""
        tool = copilot_ops._search_tool()
        self.check(tool["filters"]["blocked_domains"] == [],
                   "an empty override should clear the list, not disable the filter key")


if __name__ == "__main__":
    unittest.main(verbosity=2)
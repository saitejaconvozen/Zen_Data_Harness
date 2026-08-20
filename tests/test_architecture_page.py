"""The architecture page must not misdescribe the pipeline.

The published schematic said "NINE STAGES, ONE ROUTER" and then listed five of
them. There are ten. A diagram that contradicts the code is worse than no
diagram, because a reader trusts it over the source — and nothing else in the
build would have caught it.
"""

from __future__ import annotations

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "web" / "static" / "architecture.html"
WORKER = ROOT / "src" / "zen_agent" / "factory_worker.py"

NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}


def declared_stages() -> set[str]:
    block = re.search(r"^STAGE_TOOLS\s*=\s*\{(.*?)^\}", WORKER.read_text(encoding="utf-8"),
                      re.S | re.M)
    assert block, "STAGE_TOOLS not found"
    return set(re.findall(r'"([a-z_]+)"\s*:', block.group(1)))


class ArchitecturePageTests(unittest.TestCase):
    def setUp(self) -> None:
        if not PAGE.is_file():
            self.skipTest("architecture page not built")
        self.page = PAGE.read_text(encoding="utf-8")
        self.stages = declared_stages()

    def test_every_stage_is_named_on_the_page(self) -> None:
        missing = sorted(s for s in self.stages if s not in self.page)
        self.assertEqual(missing, [], f"stages absent from the schematic: {missing}")

    def test_the_stated_stage_count_is_correct(self) -> None:
        claim = re.search(r"\b([A-Za-z]+)\s+STAGES\b", self.page, re.I)
        self.assertIsNotNone(claim, "page does not state a stage count")
        word = claim.group(1).lower()
        self.assertIn(word, NUMBER_WORDS, f"unrecognised count word {word!r}")
        self.assertEqual(
            NUMBER_WORDS[word], len(self.stages),
            f"page claims {word} stages; STAGE_TOOLS declares {len(self.stages)}",
        )


if __name__ == "__main__":
    unittest.main()

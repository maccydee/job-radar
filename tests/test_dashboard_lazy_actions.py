"""The action bar is built for the rows on screen, and on touch for the rest.

Nine controls a row across 4,191 rows was most of a 7.2MB document and around
60,000 nodes, every one built before the browser would paint, for rows nobody
had scrolled to. Measured on Callum's board: 98,009 nodes to 59,158 and 7.2MB
to 5.2MB, with `domContentLoaded` 1,258ms to 802ms.

The risk this introduces is drift. Two renderers of one control is exactly how
this dashboard once shipped a status menu offering two of its ten statuses, so
the Python that writes the eager rows and the JavaScript that builds the lazy
ones are checked against each other here.
"""

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobradar.output import interactive

SRC = Path(interactive.__file__).read_text(encoding="utf-8")
JS = interactive._JS


def _acts_python() -> str:
    # From the `b()` helper, because that and `letter_btn` are where two of
    # the three generate buttons are actually named.
    start = SRC.index("    def b(kind, label")
    return SRC[start:SRC.index("\n    return (", start)]


def _fill_acts_js() -> str:
    start = JS.index("function fillActs(")
    return JS[start:JS.index("\ndocument.addEventListener", start)]


class LazyActions(unittest.TestCase):
    def test_only_the_first_rows_are_written_with_their_buttons(self):
        self.assertGreater(interactive.EAGER_ROWS, 0)
        self.assertIn("eager=i < EAGER_ROWS", SRC)

    def test_a_lazy_row_carries_what_the_builder_needs(self):
        for attr in ("data-url", "data-hascv", "data-settled", "data-lazyacts"):
            self.assertIn(attr, SRC, f"a lazy row cannot be rebuilt without {attr}")

    def test_both_renderers_offer_the_same_actions(self):
        py, js = _acts_python(), _fill_acts_js()
        # The Python side names the kind through the `b()` helper, which is
        # what writes the data-gen attribute; the JS writes it literally.
        # Both sides name the kind; only the eager one writes the attribute
        # as a literal, since the lazy side builds it from the kind at
        # runtime. What has to match is the set of kinds offered.
        for kind in ("screen", "cv", "cover_letter"):
            self.assertIn(f'"{kind}"', py, f"the eager rows offer no {kind}")
            self.assertIn(f"'{kind}'", js, f"the lazy rows offer no {kind}")
        self.assertIn('data-gen="', js)
        for attr in ('data-status="skipped"', 'data-status="interested"',
                     'data-note="1"'):
            self.assertIn(attr, py)
            self.assertIn(attr, js)
        # The status control, however each side spells it into the DOM.
        self.assertIn('class="setstatus"', py)
        self.assertIn("className='setstatus'", js)
        self.assertIn("data-apply", py)
        self.assertIn("apply", js)

    def test_a_row_is_filled_on_touch_as_well_as_hover(self):
        # A touch screen has no hover. A row whose buttons were never built
        # would swallow the tap in silence.
        for evt in ("pointerover", "focusin", "pointerdown"):
            self.assertIn(f"'{evt}'", JS)


class ViewSurvivesAReload(unittest.TestCase):
    """The board reloads itself when a screen finishes. It used to land the
    reader back on Open with four thousand rows, having just paid to narrow to
    eleven."""

    def test_the_view_is_saved_and_restored(self):
        self.assertIn("function saveView()", JS)
        self.assertIn("function loadView()", JS)
        self.assertIn("loadView();", JS)
        self.assertIn("saveView();", JS)

    def test_storage_access_is_guarded(self):
        # A browser set to block site data throws on the accessor itself.
        save = JS[JS.index("function saveView("):JS.index("function loadView(")]
        self.assertIn("try{", save)
        self.assertIn("catch", save)

    def test_the_view_outlives_the_tab(self):
        # sessionStorage was the first attempt. It survived the board's own
        # reloads and lost the view every time the browser closed, which is
        # most of when remembering it matters.
        save = JS[JS.index("function saveView("):JS.index("function loadView(")]
        load = JS[JS.index("function loadView("):JS.index("const VIEW", JS.index("function loadView(")) if False else JS.index("function loadView(")+900]
        self.assertIn("localStorage", save)
        self.assertIn("localStorage", load)
        self.assertNotIn("sessionStorage", save)

    def test_a_restored_filter_must_still_exist_on_the_page(self):
        # A sector whose last role went settled has no chip this time. A set
        # holding a key nothing can clear is a filter that cannot be seen or
        # switched off, and it renders as a board with nothing in it.
        # Every facet's set is pruned by one loop, so a facet added later
        # cannot be restored without being pruned. Checked per facet: the
        # status chips were the first to arrive after the loop.
        load = JS[JS.index("function loadView("):]
        load = load[:load.index("\n}")]
        self.assertIn("set.delete(x)", load)
        prune = load[load.rindex("for(const [set,dim] of"):]
        for pair in ("[secs,'sec']", "[modes,'mode']", "[emps,'emp']",
                     "[sts,'st']"):
            self.assertIn(pair, prune)


if __name__ == "__main__":
    unittest.main()

"""sync-data-Patches: Gelesen-Marken, Sammlungen und Lesefortschritt.

Reine Umformung der Cloud-Antworten -- keine Stubs nötig. Geprüft wird, was
in einem PATCH landet und was der Nutzer danach als gelesen bzw. Fortschritt
sieht.
"""
import unittest

from ..errors import TolinoApiError
from ..sync_data import (READ_TAG, active_tags, collection_pairs, read_ids,
                         reading_progress, tag_id_from_patch, tag_patch)


def tag(book_id, name, category, tag_id=None, op="add", revision=None):
    """Ein Tag-Patch in der Form, die die Cloud zurückliefert."""
    path = "/publications/%s/tags" % book_id
    if tag_id:
        path = "%s/%s" % (path, tag_id)
    value = {"name": name, "category": category}
    if revision is not None:
        value["revision"] = revision
    return {"op": op, "path": path, "value": value}


def bookmark(book_id, bookmark_id, modified, current=None, last=None,
             progress=None, op="add", position=""):
    value = {"modified": modified, "position": position}
    if current is not None:
        value["currentPosition"] = current
    if last is not None:
        value["lastPosition"] = last
    if progress is not None:
        value["progress"] = progress
    return {"op": op, "value": value,
            "path": "/publications/%s/bookmark/%s" % (book_id, bookmark_id)}


class TagPatchTests(unittest.TestCase):
    """Ein Patch braucht Pfad, Kategorie und beim Entfernen die Marken-ID."""

    def test_adding_needs_no_tag_id_and_carries_a_timestamp(self):
        patch = tag_patch("add", "d1", "Grenzwelten", "collection")
        self.assertEqual("add", patch["op"])
        self.assertEqual("/publications/d1/tags", patch["path"])
        self.assertEqual(("Grenzwelten", "collection"),
                         (patch["value"]["name"], patch["value"]["category"]))
        self.assertGreater(patch["value"]["modified"], 1_600_000_000_000)

    def test_removing_without_a_tag_id_is_an_error(self):
        with self.assertRaises(TolinoApiError) as caught:
            tag_patch("remove", "d1", READ_TAG, "system")
        self.assertEqual("N02", caught.exception.code)
        self.assertIn("Marken-ID", str(caught.exception))

    def test_removing_addresses_the_exact_tag(self):
        patch = tag_patch("remove", "d1", READ_TAG, "system",
                          revision=4, tag_id="tag-9")
        self.assertEqual("/publications/d1/tags/tag-9", patch["path"])
        self.assertEqual(4, patch["value"]["revision"])
        self.assertEqual("tag-9", tag_id_from_patch(patch))


class ActiveTagTests(unittest.TestCase):
    """Die Historie zählt: eine spätere Entfernung hebt die Marke auf."""

    def test_the_latest_add_wins_and_removal_clears_it(self):
        patches = [tag("d1", "Grenzwelten", "collection", "tag-1"),
                   tag("d1", "Grenzwelten", "collection", "tag-2")]
        active = active_tags(patches, "collection")
        self.assertEqual([("d1", "Grenzwelten")], list(active))
        self.assertEqual("/publications/d1/tags/tag-2",
                         active[("d1", "Grenzwelten")]["path"])
        patches.append(tag("d1", "Grenzwelten", "collection", "tag-2",
                           op="remove"))
        self.assertEqual({}, active_tags(patches, "collection"))

    def test_other_categories_names_and_broken_paths_are_ignored(self):
        patches = [tag("d1", "Grenzwelten", "system"),
                   tag("d1", "Andere Serie", "collection"),
                   {"op": "add", "value": {"name": "Serie",
                                           "category": "collection"},
                    "path": "/publications/"},
                   "kein Patch",
                   {"op": "add", "value": {}, "path": "/publications/d9/tags"}]
        self.assertEqual({"d1": [("d1", "Grenzwelten")]},
                         {"d1": list(active_tags(patches, "system"))})
        self.assertEqual([("d1", "Andere Serie")],
                         list(active_tags(patches, "collection")))

    def test_read_marks_count_in_both_cloud_spellings(self):
        patches = [tag("d1", READ_TAG, "system"),
                   tag("d2", READ_TAG, "collection"),
                   tag("d3", "Grenzwelten", "collection")]
        self.assertEqual({"d1", "d2"}, set(read_ids(patches)))

    def test_collections_are_pairs_of_book_and_name(self):
        patches = [tag("d1", "Grenzwelten", "collection"),
                   tag("d2", "Grenzwelten", "collection"),
                   tag("d1", "Grenzwelten", "collection", "tag-1", op="remove")]
        self.assertEqual({("d2", "Grenzwelten")}, collection_pairs(patches))


class ReadingProgressTests(unittest.TestCase):
    """Angezeigt wird das zuletzt geänderte noch vorhandene Lesezeichen."""

    def test_progress_comes_from_a_fraction_or_from_positions(self):
        patches = [bookmark("d1", "b1", 10, progress=0.426),
                   bookmark("d2", "b2", 10, current="12", last="300"),
                   bookmark("d3", "b3", 10, progress=150),
                   bookmark("d4", "b4", 10, progress="unbekannt")]
        progress = reading_progress(patches)
        self.assertEqual(43, progress["d1"]["percent"])
        self.assertEqual(4, progress["d2"]["percent"])
        # Ohne Prozentwert und ohne Positionen gibt es nichts anzuzeigen.
        self.assertNotIn("d3", progress)
        self.assertNotIn("d4", progress)

    def test_percent_is_clamped_to_hundred(self):
        progress = reading_progress([bookmark("d1", "b1", 10, progress=0.999)])
        self.assertEqual(100, progress["d1"]["percent"])

    def test_the_newest_bookmark_of_a_book_is_shown(self):
        patches = [bookmark("d1", "alt", 100, progress=0.2,
                            position="Kapitel 2"),
                   bookmark("d1", "neu", 200, progress=0.8)]
        progress = reading_progress(patches)["d1"]
        self.assertEqual((80, ""), (progress["percent"], progress["position"]))

    def test_removing_the_newest_bookmark_falls_back_to_the_older_one(self):
        patches = [bookmark("d1", "alt", 100, progress=0.2),
                   bookmark("d1", "neu", 200, progress=0.8),
                   bookmark("d1", "neu", 200, op="remove")]
        self.assertEqual(20, reading_progress(patches)["d1"]["percent"])

    def test_a_removal_only_drops_its_own_bookmark(self):
        patches = [bookmark("d1", "alt", 100, progress=0.2),
                   bookmark("d1", "neu", 200, progress=0.8),
                   bookmark("d1", "alt", 100, op="remove")]
        self.assertEqual(80, reading_progress(patches)["d1"]["percent"])


if __name__ == "__main__":
    unittest.main()

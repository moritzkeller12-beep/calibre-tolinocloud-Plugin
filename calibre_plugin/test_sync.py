import unittest

from .sync import fingerprint, plan_sync
from .tolino import PARTNERS, hardware_id


class SyncPlanTests(unittest.TestCase):
    def setUp(self):
        self.book = {"uuid": "u1", "title": "A", "formats": ["EPUB"], "last_modified": "1"}

    def test_new_book_is_uploaded(self):
        uploads, removals, state = plan_sync({1: self.book}, {}, ["EPUB"])
        self.assertEqual([(1, "u1", "EPUB", None)], uploads)
        self.assertFalse(removals)
        self.assertIn("u1", state)

    def test_unchanged_book_is_not_uploaded(self):
        old = {"u1": {"tolino_id": "t1", "fingerprint": fingerprint(self.book, "EPUB")}}
        self.assertEqual([], plan_sync({1: self.book}, old, ["EPUB"])[0])

    def test_changed_book_is_uploaded(self):
        old = {"u1": {"tolino_id": "t1", "fingerprint": "old"}}
        self.assertEqual("t1", plan_sync({1: self.book}, old, ["EPUB"])[0][0][3])

    def test_deletions_are_opt_in(self):
        state = {"gone": {"tolino_id": "t2", "fingerprint": "x"}}
        self.assertEqual([], plan_sync({}, state, ["EPUB"])[1])
        self.assertEqual([("gone", "t2")], plan_sync({}, state, ["EPUB"], True)[1])

    def test_reference_partners_and_stable_hardware_shape(self):
        for partner_id in (3, 4, 6, 8, 13, 23, 30):
            self.assertIn(partner_id, PARTNERS)
        self.assertRegex(hardware_id(), r"^[123x]..A-00BCD-EFGHI-JKLMN-OPQRh$")


if __name__ == "__main__":
    unittest.main()

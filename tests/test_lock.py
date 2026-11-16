import tests  # noqa: F401  (a temporary data folder, never the real one)
import unittest
from unittest import mock

from margin import lock


class Lock(unittest.TestCase):
    def setUp(self):
        lock.lock_now()
        self.now = 1_000_000.0
        self.p = mock.patch("margin.lock.time.time", lambda: self.now)
        self.p.start()

    def tearDown(self):
        self.p.stop()
        lock.lock_now()

    def test_starts_locked_and_still_here_cannot_unlock(self):
        s = {"lock_minutes": 2}
        self.assertTrue(lock.locked(s))
        self.assertTrue(lock.still_here(s))
        self.assertTrue(lock.locked(s))

    def test_locks_two_minutes_after_the_page_stops_checking_in(self):
        s = {"lock_minutes": 2}
        with mock.patch("margin.lock.subprocess.run", return_value=mock.Mock(returncode=0)), \
                mock.patch("margin.lock._binary", return_value="Margin"):
            self.assertEqual(lock.unlock(s), (True, ""))
        self.now += 90
        self.assertFalse(lock.still_here(s))      # in front of you: another 2 minutes from now
        self.now += 110
        self.assertFalse(lock.locked(s))
        self.now += 15                            # 125 s since the last check-in
        self.assertTrue(lock.locked(s))

    def test_cancelled_touch_id_stays_locked(self):
        s = {"lock_minutes": 2}
        with mock.patch("margin.lock.subprocess.run", return_value=mock.Mock(returncode=1)), \
                mock.patch("margin.lock._binary", return_value="Margin"):
            self.assertEqual(lock.unlock(s), (False, "Cancelled"))
        self.assertTrue(lock.locked(s))

    def test_lock_button_and_off_switch(self):
        lock._STATE["until"] = self.now + 100
        self.assertFalse(lock.locked({}))
        lock.lock_now()
        self.assertTrue(lock.locked({}))
        self.assertFalse(lock.locked({"lock_minutes": 0}))


if __name__ == "__main__":
    unittest.main()

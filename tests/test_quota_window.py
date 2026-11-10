"""What "120 requests per minute" actually means at the throttle.

These pin the shape of the window rather than the number, because the number is
what someone tunes and the shape is what they then assume. The window is fixed
and anchored at the first request after a quiet minute - not rolling - so a
client can spend roughly twice the limit across a boundary. If you change the
algorithm, change these and README §3 together.

The clock is faked, so a "minute" costs nothing.
"""

import time

import api_proxy


class Clock:
    """api_proxy calls time.time/strftime/gmtime, so replace the whole name."""

    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def time(self):
        return self.t

    def strftime(self, fmt, g):
        return time.strftime(fmt, g)

    def gmtime(self, t):
        return time.gmtime(t)


def throttle(monkeypatch, rate):
    monkeypatch.setattr(api_proxy, "RATE_PER_MIN", rate)
    monkeypatch.setattr(api_proxy, "time", Clock())
    api_proxy._rl.clear()
    return api_proxy


def test_budget_refills_only_after_a_quiet_minute(monkeypatch):
    gw = throttle(monkeypatch, 3)
    assert [gw.check_quota("laptop")[0] for _ in range(3)] == [True, True, True]
    assert gw.check_quota("laptop") == (False, "rate limit: 3 requests/min")
    gw.time.t += 30  # half the window gone by, still no budget
    assert gw.check_quota("laptop")[0] is False
    gw.time.t += 31  # 61 s since the window opened
    assert gw.check_quota("laptop")[0] is True


def test_2x_the_limit_fits_across_a_window_boundary(monkeypatch):
    """The one consequence worth writing down before someone 'fixes' it.

    Six requests in 61 s against a limit of 3/min: a rolling window would allow
    three. A serial engine with --max-active-requests 6 queues the difference,
    which is why this is documented rather than corrected.
    """
    gw = throttle(monkeypatch, 3)
    assert all(gw.check_quota("laptop")[0] for _ in range(3))  # spent at t=0
    gw.time.t += 59
    assert gw.check_quota("laptop")[0] is False
    gw.time.t += 2  # boundary crossed inside the client's stride
    assert all(gw.check_quota("laptop")[0] for _ in range(3))


def test_each_key_has_its_own_budget(monkeypatch):
    gw = throttle(monkeypatch, 1)
    assert gw.check_quota("laptop")[0] is True
    assert gw.check_quota("phone")[0] is True  # one key cannot starve another
    assert gw.check_quota("laptop")[0] is False

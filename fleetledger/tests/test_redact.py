"""redact_emails(): the same output as the plain email pattern, in linear time."""

import random
import re
import time
from pathlib import Path

from fleetledger.concierge.redact import clean, redact, redact_emails

# The pattern redact() used before: correct, but it re-scans a long run of
# local-part characters from every one of its positions.
PLAIN = re.compile(r"[\w.+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
FIXTURES = Path(__file__).parent / "fixtures" / "concierge"


def plain(text: str) -> str:
    return PLAIN.sub("[email]", text)


def test_back_to_back_addresses_are_all_redacted():
    # A match can end on a character that may also start the next local part.
    for text in ("a@example.com_c@example.org", "a@example.com+c@example.org",
                 "a@example.com.@example.org", "a@example.comé@example.org",
                 "x a@example.com_c@example.org_e@example.net y",
                 "a@example@example.org"):
        assert redact_emails(text) == plain(text), text


def test_random_text_matches_the_plain_pattern():
    rng = random.Random(20261110)
    alphabet = "ab_.+-@ é1\n/:?"
    for _ in range(20000):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randrange(40)))
        assert redact_emails(text) == plain(text), repr(text)


def test_fixture_logs_match_the_plain_pattern():
    texts = [clean(p.read_text(errors="replace")) for p in sorted(FIXTURES.glob("*.txt"))]
    texts.append("Logged in as first.last+tag@mail.corp.example, x@a.b.test and "
                 "u_1@ship-01.mail.invalid.")
    for text in texts:
        assert redact_emails(text) == plain(text)


def test_a_long_run_without_an_address_is_quick():
    for text in ("x" * 20000, "x" * 20000 + "@", ("x" * 5000 + "@") * 4):
        start = time.perf_counter()
        redact(text)
        assert time.perf_counter() - start < 0.2, len(text)  # the plain pattern takes seconds

"""Which ENGINE is running: the guarded one, or the original.

Not to be confused with `app.strategy.profiles`, which names research *strategy*
candidates and is evaluated offline. This module names the two engine builds
that can actually drive the account, and nothing else.

The grid is the same in both. `tests/test_engine_profiles.py` pins that: the
levels, the spacing, the lot, the basket target and the next-candle gate come
out identical for identical settings. What differs is how much has to be known,
and how much has to be set, before a grid may be placed at all — and what
happens to exposure after a limit is breached.

Choosing between them is therefore not a strategy choice. It is a choice about
how much the engine is willing to assume.
"""

from __future__ import annotations

from dataclasses import dataclass

GUARDED = "guarded"
ORIGINAL = "original"


@dataclass(frozen=True)
class EngineProfile:
    key: str
    label: str
    summary: str
    #: Protection this profile HAS that the other does not. Stated as a list so
    #: the dashboard can show the difference rather than a word.
    adds: tuple[str, ...]
    #: True for the profile whose refusals are the ones this repository's tests
    #: and documents describe.
    guarded: bool

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "summary": self.summary,
            "adds": list(self.adds),
            "guarded": self.guarded,
        }


GUARDED_PROFILE = EngineProfile(
    key=GUARDED,
    label="Guarded engine (current)",
    summary=(
        "Today's engine. The same grid, with admission gates in front of it: a "
        "grid is refused unless the money arithmetic behind it can be done from "
        "values that were actually read or stated, never assumed."
    ),
    adds=(
        "capital floor: an entry rule on balance and an active trigger on equity",
        "closing-cost contract: unknown exit costs block a new basket instead of "
        "counting as zero",
        "symbol valuation: an unknown tick value, point size or quote refuses "
        "entry rather than being replaced with a default",
        "liquidation policy: exposure appearing after a risk halt is cancelled "
        "and closed again, across restarts, until an owner resumes",
        "marked daily risk: the daily limit is judged on a reading that includes "
        "floating loss, not only settled results",
        "owner pause, resume and close-and-pause, separate from Stop",
    ),
    guarded=True,
)

ORIGINAL_PROFILE = EngineProfile(
    key=ORIGINAL,
    label="Original bot (no gate)",
    summary=(
        "The bot as it was at commit 1c7d62d, before the risk work started, "
        "taken from git rather than rewritten. No entry gate of any kind, and a "
        "profitable basket is replaced on the same candle that closed it. It is "
        "what produced the trade history already in this database."
    ),
    adds=(),
    guarded=False,
)

ALL_PROFILES = {p.key: p for p in (GUARDED_PROFILE, ORIGINAL_PROFILE)}

#: The engine a fresh install runs. It is the ORIGINAL one, chosen by the owner:
#: they asked for the bot they had before the risk work, without the capital
#: floor and the capital reserve that refuse a grid on a small account, and with
#: the same-candle restart after a profitable basket. `normalise` below still
#: resolves an UNRECOGNISED value to the guarded engine rather than to this one,
#: because a typo in a settings file is not that choice being made again.
DEFAULT_PROFILE = ORIGINAL


def normalise(value) -> str:
    """The profile key for a stored or submitted value.

    An unrecognised value becomes the GUARDED profile, never the original one -
    which is NOT the same as `DEFAULT_PROFILE`, and deliberately so. A settings
    file written by a future version, or a typo in an API call, must not be a
    way to end up running with fewer refusals; only an explicit choice selects
    the engine that has none.
    """
    if isinstance(value, str) and value.strip().lower() in ALL_PROFILES:
        return value.strip().lower()
    return GUARDED


def describe(key: str) -> dict:
    return ALL_PROFILES[normalise(key)].as_dict()

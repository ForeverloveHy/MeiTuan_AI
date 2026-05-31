from __future__ import annotations

CORE_VERSION = "fix70"
CORE_VERSION_NOTE = (
    "fix70: preserves transcript evidence for arbitration, adds an offline graph "
    "evaluation demo app, and keeps version-stamped reports."
)


def runtime_version_info() -> dict[str, str]:
    return {
        "core_version": CORE_VERSION,
        "core_version_note": CORE_VERSION_NOTE,
        "version_module": __file__,
    }

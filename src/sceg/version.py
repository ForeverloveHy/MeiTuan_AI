from __future__ import annotations

# Stable kernel label for reports.  This is not a historical version stamp; it
# identifies the current method contract used by the local evaluator.
CORE_VERSION = "element-contract-kernel"
CORE_VERSION_NOTE = (
    "一图两表 + Atom/Element Groups + main/pool recall + fact/value_check + "
    "trigger-driven branches + local second filter arbitration."
)


def runtime_version_info() -> dict[str, str]:
    return {
        "core_version": CORE_VERSION,
        "core_version_note": CORE_VERSION_NOTE,
        "version_module": __file__,
    }

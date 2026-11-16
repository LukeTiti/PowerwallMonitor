"""Sanity-checks readings for anything that looks wrong."""

# TODO(you): implement the actual "does this reading look wrong" check.
#
# Why this is yours to write: this is exactly the judgment call the Tesla
# app is getting wrong, so it's worth deciding deliberately rather than
# copying a generic threshold. Some angles to consider:
#
#   - Energy conservation: solar_w should roughly equal
#     load_w + battery charge/discharge + grid import/export. If those
#     don't balance within some tolerance, something's mislabeled.
#   - Physical limits: solar_w above your system's rated capacity (check
#     your install paperwork) is impossible — a good sign the value is
#     actually the load or grid reading mislabeled as solar.
#   - Sign flips: e.g. battery_w and grid_w swapping sign between
#     consecutive readings for no physical reason (batteries don't
#     instantly reverse).
#   - How much tolerance to give — sensors have noise and reporting lag,
#     so too tight a check spams false alerts; too loose misses real bugs.
#
# `history` is the list of prior readings this run (oldest first), in case
# you want to compare against recent trend rather than just the one sample.
#
# Return None if the reading looks fine, or a short string describing the
# problem if it should trigger an alert.
def check_for_anomaly(reading: dict, history: list[dict]) -> str | None:
    raise NotImplementedError("fill in your own sanity check here")

from __future__ import annotations

from enum import StrEnum


class SolType(StrEnum):
  statics = "statics"
  modes = "modes"
  buckling = "buckling"
  nonlinear_statics = "nonlinear_statics"
  differential_stiffness = "differential_stiffness"
  craig_bampton = "craig_bampton"
  # complex_modes = "complex_modes"
  # frequency_direct = "frequency_direct"
  # frequency_modal = "frequency_modal"
  # transient_modal = "transient_modal"
  # transient_direct = "transient_direct"
  # heat_transfer = "heat_transfer"
  # flutter = "flutter"
  unknown = "unknown"


_SOL_ALIASES: dict[str, SolType] = {
  alias: sol_type
  for sol_type, aliases in [
    (SolType.statics, ["101", "1", "statics", "static"]),
    (
      SolType.modes,
      ["103", "3", "modes", "mode", "eigen", "eigenvalues", "normal_modes"],
    ),
    (SolType.differential_stiffness, ["4", "104", "differen"]),
    (SolType.buckling, ["105", "5", "buckling", "buckling_modes"]),
    (
      SolType.nonlinear_statics,
      ["106", "66", "nonlinear", "nlstatics", "nlstatic", "nonlinear_statics"],
    ),
    (SolType.craig_bampton, "31"),
    # (
    #   SolType.complex_modes,
    #   ["107", "complex_eigen", "complex_modes", "damped_modes"],
    # ),
    # (
    #   SolType.frequency_direct,
    #   [
    #     "108",
    #     "direct_frequency",
    #     "freq_direct",
    #     "frequency_response_direct",
    #   ],
    # ),
    # (
    #   SolType.frequency_modal,
    #   [
    #     "109",
    #     "modal_frequency",
    #     "freq_modal",
    #     "frequency_response_modal",
    #   ],
    # ),
    # (SolType.transient_modal, ["111", "modal_transient", "transient_modal"]),
    # (
    #   SolType.transient_direct,
    #   ["112", "direct_transient", "transient_direct"],
    # ),
    # (SolType.heat_transfer, ["153", "heat", "heat_transfer", "thermal"]),
    # SolType.flutter, ["145", "flutter"]),
  ]
  for alias in aliases
}


def normalize_sol(raw: str | None) -> SolType | None:
  """Return the canonical SolType for a raw SOL string.

  Returns None if raw is None (no SOL line found).
  Returns SolType.unknown if the value is not in the lookup table.
  Spaces and underscores are treated as equivalent (e.g. "heat transfer"
  matches "heat_transfer").
  """
  if raw is None:
    return None
  normalised = raw.lower().strip().replace(" ", "_")
  return _SOL_ALIASES.get(normalised, SolType.unknown)

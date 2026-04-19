from __future__ import annotations

from pathlib import Path


def build_command(
  binary: Path,
  deck_path: Path,
  valgrind_xml_path: Path,
) -> list[str]:
  """Return the argv list for a sandboxed MYSTRAN run.

  Wraps MYSTRAN in valgrind memcheck (inside firejail for network and
  some syscall isolation).  The process should be launched with
  ``cwd`` set to the run's work directory so that MYSTRAN writes all
  output files there.

  Args:
    binary: Absolute path to the MYSTRAN executable.
    deck_path: Absolute path to the input deck file (inside the work dir).
    valgrind_xml_path: Where valgrind should write its XML output.

  Returns:
    A list of strings suitable for ``asyncio.create_subprocess_exec``.
  """
  return [
    "firejail",
    "--net=none",
    "--quiet",
    # ↑ firejail options
    "valgrind",
    "--tool=memcheck",
    "--xml=yes",
    f"--xml-file={valgrind_xml_path}",
    # ↑ valgrind options
    str(binary),
    str(deck_path),
    # ↑ MYSTRAN invocation
  ]

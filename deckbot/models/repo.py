"""Approved MYSTRAN repositories.

Keys are short human-readable names used throughout the codebase and stored
in ``mystran_versions.repo_name``.  Values are the git clone URLs used for
``git ls-remote`` and node builds.

Add entries here to allow users to reference a repository in ``/deck run``.
"""

APPROVED_REPOS: dict[str, str] = {
  "mystran": "https://github.com/MYSTRANsolver/MYSTRAN.git",
}

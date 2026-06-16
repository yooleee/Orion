# =============================================================================
# orion/__init__.py
# -----------------------------------------------------------------------------
# Responsible for: Marking `orion` as a package and exposing the version string.
# Role in project: __version__ is stamped into every ReportBlob so a future
#                  shared service could tell which Orion produced an old report.
# =============================================================================

# Pre-release placeholder, kept in lock-step with pyproject.toml. Orion is
# unversioned during development (progress is tracked by phase — see CHANGELOG.md),
# so this stays "0.0.0" until the first published release. It is still stamped into
# every ReportBlob as provenance, so a future shared service can tell which build
# produced an old report even before formal versioning begins.
__version__ = "0.0.0"

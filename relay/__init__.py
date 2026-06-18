# =============================================================================
# relay/ — the hosted half of Orion (C1 reference implementation)
# -----------------------------------------------------------------------------
# Responsible for: Receiving the portable report blob that local Orion pushes,
#                  storing it, and serving a read-only web dashboard from it.
# Role in project: This is the "Path-B reference" hosted relay — a small, portable
#                  Python app that is deployable SEPARATELY from the `orion` pip
#                  package (it is intentionally NOT part of the src/ wheel). It
#                  depends on nothing in `orion`: local Orion and this relay share
#                  only the portable blob contract (serialize_blob's JSON), not
#                  code. That independence is what keeps the hosting choice open —
#                  this package could run on any container host, or be reimplemented
#                  on another stack, without touching local Orion.
# Modules: store (own SQLite store), server (ingest + routing), render (HTML).
# =============================================================================

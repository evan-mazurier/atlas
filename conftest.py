"""Test isolation: the ledger outside-anchor check fetches from a pinned GitHub remote, which a test run must never do.
Anchor tests turn it back on with their own temp config (ATLAS_ANCHOR_DISABLE=0)."""
import os

os.environ.setdefault("ATLAS_ANCHOR_DISABLE", "1")

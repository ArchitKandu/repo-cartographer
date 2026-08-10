"""Repo Cartographer's tests.

A package rather than a bare directory, and only since Phase 5. The eval set
lives at `tests/evals/`, and its dataset loader and scorer are imported from two
places that are not each other — `tests/test_evals.py` and
`scripts/run_evals.py` — so they need a real, unambiguous import path
(`tests.evals.dataset`) rather than whatever pytest's rootdir insertion happens
to make available. `pyproject.toml`'s `pythonpath = ["."]` is what makes that
path resolve.
"""

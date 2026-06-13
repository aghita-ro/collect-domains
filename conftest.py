import sys
import os

# Ensure the repo root is on sys.path so that `webhook` and `scraper` are importable
# when pytest is invoked as a binary (not via `python -m pytest`).
sys.path.insert(0, os.path.dirname(__file__))

"""Shared pytest fixtures.

Sets placeholder values for required env vars so `src.main` can be imported
in tests without an actual `.env` file.
"""
import os

os.environ.setdefault("DEVIN_API_KEY", "test-devin-key")
os.environ.setdefault("GITHUB_TOKEN", "test-github-token")
os.environ.setdefault("GITHUB_WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("NGROK_API_URL", "")

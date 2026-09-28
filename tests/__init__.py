"""Test package. Point the pipeline logger at a throwaway directory before any pipeline module is
imported, so test runs never appear as runs in data/logs/events.jsonl (and in the console)."""
import os
import tempfile

os.environ.setdefault("PIPELINE_LOG_DIR", tempfile.mkdtemp(prefix="pipeline-test-logs-"))
# Same for your stocks and message preferences: tests read a fixed portfolio and never your own files.
os.environ["BURSA_PORTFOLIO"] = os.path.join(os.path.dirname(__file__), "fixtures", "portfolio.yaml")
os.environ["BURSA_PREFS"] = os.path.join(tempfile.mkdtemp(prefix="pipeline-test-prefs-"), "preferences.yaml")

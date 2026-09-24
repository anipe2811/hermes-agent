import os
import tempfile

# Settings are read at import time, so configure the environment first.
os.environ.setdefault("OPENROUTER_API_KEY", "test-openrouter-key")
os.environ["HERMES_API_KEY"] = "test-hermes-key"
os.environ["SESSION_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "sessions.db")
os.environ["RATE_LIMIT_PER_MINUTE"] = "1000"
os.environ["GLOBAL_RATE_LIMIT_PER_MINUTE"] = "1000"
os.environ["BRAVE_API_KEY"] = ""

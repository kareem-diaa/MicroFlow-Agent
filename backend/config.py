"""Central configuration. Every value can be overridden with an environment variable."""
from __future__ import annotations

import base64
import os
import secrets
import warnings
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
MODEL_DIR = BASE_DIR / "models"
MODEL_DIR.mkdir(exist_ok=True)

DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{BASE_DIR / 'microflow.db'}")
REDIS_URL = os.getenv("REDIS_URL", "")  # optional; in-memory cache is used when empty

# --- Secrets -----------------------------------------------------------------
# In production these come from a secret manager (GCP Secret Manager / AWS KMS).
# For the local demo we generate ephemeral values and warn loudly.
_master = os.getenv("MICROFLOW_MASTER_KEY")
if _master:
    MASTER_KEY = base64.b64decode(_master)
    if len(MASTER_KEY) != 32:
        raise RuntimeError("MICROFLOW_MASTER_KEY must be 32 bytes, base64-encoded")
else:
    _key_file = BASE_DIR / ".dev_master_key"
    if _key_file.exists():
        MASTER_KEY = base64.b64decode(_key_file.read_text().strip())
    else:
        MASTER_KEY = secrets.token_bytes(32)
        _key_file.write_text(base64.b64encode(MASTER_KEY).decode())
        try:
            _key_file.chmod(0o600)
        except OSError:
            pass
    warnings.warn(
        "MICROFLOW_MASTER_KEY not set: using a local development key file. "
        "Never use this mode in production.",
        stacklevel=1,
    )

JWT_SECRET = os.getenv("MICROFLOW_JWT_SECRET") or base64.b64encode(MASTER_KEY[::-1]).decode()
JWT_ALGORITHM = "HS256"
JWT_TTL_MINUTES = int(os.getenv("MICROFLOW_JWT_TTL_MINUTES", "60"))

# --- Input limits --------------------------------------------------------------
MAX_UPLOAD_BYTES = int(os.getenv("MICROFLOW_MAX_UPLOAD_BYTES", str(5 * 1024 * 1024)))
MAX_LOGIN_FAILURES = 5
LOCKOUT_MINUTES = 15

# --- Demo users (development seed only) ---------------------------------------
DEMO_PASSWORD = os.getenv("MICROFLOW_DEMO_PASSWORD", "Demo@2026!")
SEED_DEMO_USERS = os.getenv("MICROFLOW_SEED_DEMO", "1") == "1"

# --- Business policy (illustrative parameters, editable, not legal advice) ----
POLICY = {
    "min_history_months": 3,
    "max_repayment_to_inflow_ratio": 0.35,
    "min_score_for_auto_draft": 45,
    "human_review_required_below_score": 60,
    "simplified_tax_turnover_threshold_egp": 20_000_000,
    "low_cash_buffer_days": 7,
    "forecast_weeks": 13,
}

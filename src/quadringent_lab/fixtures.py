"""Fixed synthetic identities, identifiers and canaries of the laboratory.

Every value here is fictional. Hosts use the reserved ``.invalid`` TLD, the
password is a laboratory-only passphrase, and canary strings exist only to
prove that exports never contain them.
"""

from __future__ import annotations

import base64
import hashlib

from . import LAB_OTHER_ORG_ID, LAB_ORG_ID

# --- Identities (fictional) --------------------------------------------------
OPERATOR_EMAIL = "operator@lab.invalid"  # role "admin": may operate and administer
READER_EMAIL = "reader@lab.invalid"  # role "reader": read-only
OTHER_ADMIN_EMAIL = "admin@lab-other.invalid"  # administrator of the other organization
# Laboratory-only passphrase; it protects nothing outside this throwaway database.
LAB_PASSWORD = "lab-fixture-passphrase"

USER_IDS = {
    OPERATOR_EMAIL: "usr_lab_operator",
    READER_EMAIL: "usr_lab_reader",
    OTHER_ADMIN_EMAIL: "usr_other_admin",
}

# --- Deterministic identifiers ----------------------------------------------
SOURCE_ID = "src_lab_primary"
DESTINATION_ID = "dst_lab_primary"
OTHER_SOURCE_ID = "src_other_primary"
OTHER_DESTINATION_ID = "dst_other_primary"

PIPELINE_ORDERS = "pl_lab_orders"
PIPELINE_CUSTOMERS = "pl_lab_customers"
PIPELINE_OTHER_ORDERS = "pl_other_orders"

LIBRARY = "LABLIB"
SOURCE_HOST = "ibmi.lab.invalid"
SOURCE_USER = "LABUSER"
SOURCE_SECRET = "lab-fixture-ibmi-secret"
DESTINATION_ACCOUNT = "lab-fixture-account"

# Deterministic keys: the laboratory database is disposable, and a fixed key
# lets a "restarted" runtime read the same file.
SECRET_KEY = base64.urlsafe_b64encode(hashlib.sha256(b"quadringent-lab-secret-key").digest())
TOKEN_PEPPER = b"quadringent-lab-token-pepper"

# --- Canaries (scenario 19) ---------------------------------------------------
CANARY_HOST = "canary-host-4f9a2c.lab.invalid"
CANARY_USER = "CANARYUSR"
CANARY_SECRET = "CANARY-SECRET-4f9a2c"
CANARY_ACCOUNT = "canary-account-4f9a2c"
CANARY_ROW_VALUE = "CANARY-ROW-VALUE-7731"
CANARY_LOG_SECRET = "CANARY-LOG-SECRET-5512"
CANARY_EVIDENCE_SECRET = "CANARY-EVIDENCE-SECRET-9024"
CANARY_TABLE_COMMENT = "CANARY-TABLE-COMMENT-3318"

# Strings that must never appear in a diagnostic export. The private key and
# the agent token are generated at runtime; tests add them to this list.
EXPORT_FORBIDDEN_CANARIES = (
    CANARY_HOST,
    CANARY_USER,
    CANARY_SECRET,
    CANARY_ACCOUNT,
    CANARY_ROW_VALUE,
    CANARY_LOG_SECRET,
    CANARY_EVIDENCE_SECRET,
    SOURCE_SECRET,
    LAB_PASSWORD,
)

ORG_IDS = (LAB_ORG_ID, LAB_OTHER_ORG_ID)

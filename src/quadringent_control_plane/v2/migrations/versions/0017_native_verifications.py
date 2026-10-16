"""Persist native proof attempts without inferring admission for existing runs."""
from alembic import op
import sqlalchemy as sa

revision = "0017_native_verifications"
down_revision = "0016_destination_scope"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("organizations", sa.Column("native_context_enforced", sa.Boolean(), nullable=False,
                                             server_default=sa.false()))
    op.add_column("pipelines", sa.Column("native_context_generation", sa.BigInteger(), nullable=False, server_default="0"))
    op.add_column("pipelines", sa.Column("native_context_operation_id", sa.String(32), nullable=True))
    op.add_column("pipelines", sa.Column("native_context_observation_json", sa.Text(), nullable=True))
    op.add_column("pipelines", sa.Column("latest_native_verification_id", sa.String(32), nullable=True))
    op.create_table("native_verifications",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("org_id", sa.String(32), nullable=False),
        sa.Column("pipeline_id", sa.String(32), nullable=False),
        sa.Column("table_id", sa.String(32), nullable=False),
        sa.Column("source_id", sa.String(32), nullable=False),
        sa.Column("destination_id", sa.String(32), nullable=False),
        sa.Column("copy_run_id", sa.String(64), nullable=False),
        sa.Column("context_generation", sa.BigInteger(), nullable=False),
        sa.Column("context_sha256", sa.String(64), nullable=False),
        sa.Column("context_json", sa.Text(), nullable=False),
        sa.Column("scope_json", sa.Text(), nullable=True),
        sa.Column("scope_sha256", sa.String(64), nullable=True),
        sa.Column("preparation_json", sa.Text(), nullable=True),
        sa.Column("requested_by", sa.String(256), nullable=False),
        sa.Column("idempotency_key", sa.String(256), nullable=False),
        sa.Column("lifecycle", sa.String(16), nullable=False, server_default='preparing'),
        sa.Column("revision", sa.Integer(), nullable=False, server_default='0'),
        sa.Column("controller_holder", sa.String(256), nullable=True),
        sa.Column("lease_generation", sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", sa.String(16), nullable=True),
        sa.Column("reason_code", sa.String(64), nullable=True),
        sa.Column("result_manifest_json", sa.Text(), nullable=True),
        sa.Column("result_sha256", sa.String(64), nullable=True),
        sa.UniqueConstraint("org_id", "requested_by", "idempotency_key", name="uq_native_request_idempotency"),
        sa.CheckConstraint("lifecycle IN ('preparing','running','finished')", name="ck_native_lifecycle"),
        sa.CheckConstraint("(lifecycle = 'finished' AND outcome IS NOT NULL AND outcome IN ('verified','divergent','incomplete') AND finished_at IS NOT NULL) OR (lifecycle != 'finished' AND outcome IS NULL AND finished_at IS NULL)", name="ck_native_outcome"),
        sa.CheckConstraint("(outcome IN ('verified','divergent') AND result_manifest_json IS NOT NULL AND result_sha256 IS NOT NULL AND scope_sha256 IS NOT NULL) OR outcome IS NULL OR outcome = 'incomplete'", name="ck_native_manifest"),
    )
    op.create_index("ix_native_pipeline_history", "native_verifications", ["org_id", "pipeline_id", "created_at"])


def downgrade() -> None:
    raise RuntimeError("Native proof history and context fences must be preserved")

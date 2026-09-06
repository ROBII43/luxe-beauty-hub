"""Add encrypted admin settings and email MFA challenges."""

from alembic import op
import sqlalchemy as sa

revision = "0002_admin_settings_mfa"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "admin_settings",
        sa.Column("setting_key", sa.String(length=80), primary_key=True),
        sa.Column("encrypted_value", sa.Text(), nullable=False),
        sa.Column("updated_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "admin_mfa_challenges",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("code_hash", sa.String(length=128), nullable=False),
        sa.Column("token_hash", sa.String(length=128)),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("verified_at", sa.DateTime()),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_admin_mfa_challenges_user_id", "admin_mfa_challenges", ["user_id"])
    op.create_index("ix_admin_mfa_challenges_token_hash", "admin_mfa_challenges", ["token_hash"], unique=True)
    op.create_index("ix_admin_mfa_challenges_expires_at", "admin_mfa_challenges", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_admin_mfa_challenges_expires_at", table_name="admin_mfa_challenges")
    op.drop_index("ix_admin_mfa_challenges_token_hash", table_name="admin_mfa_challenges")
    op.drop_index("ix_admin_mfa_challenges_user_id", table_name="admin_mfa_challenges")
    op.drop_table("admin_mfa_challenges")
    op.drop_table("admin_settings")
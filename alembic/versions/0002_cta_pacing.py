"""cta pacing state on users

Records how often the AtreoxAI call to action has been shown to a user, so it
can stay rare instead of appearing after every job.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-10
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "cta_shown_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "users",
        sa.Column("cta_last_shown_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "cta_last_shown_at")
    op.drop_column("users", "cta_shown_count")

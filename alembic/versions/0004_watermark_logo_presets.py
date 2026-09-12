"""logo watermark presets

A preset is now either a line of text or a logo image. The image lives in the
database on purpose: the container's filesystem is ephemeral, so a logo saved
on disk would not survive a redeploy.

Existing rows are text presets, which is what the server default says.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-12
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "watermark_presets",
        sa.Column("kind", sa.String(length=8), server_default="text", nullable=False),
    )
    op.add_column(
        "watermark_presets", sa.Column("logo", sa.LargeBinary(), nullable=True)
    )
    op.add_column(
        "watermark_presets", sa.Column("logo_format", sa.String(length=8), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("watermark_presets", "logo_format")
    op.drop_column("watermark_presets", "logo")
    op.drop_column("watermark_presets", "kind")

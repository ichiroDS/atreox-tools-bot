"""watermark presets

Saved watermark texts (and the look chosen for each) per user. Written
dialect-agnostically so the same migration runs on PostgreSQL (production) and
SQLite (local development).

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-12
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "watermark_presets",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("text", sa.String(length=64), nullable=False),
        sa.Column("position", sa.String(length=16), nullable=False),
        sa.Column("style", sa.String(length=16), nullable=False),
        sa.Column("size", sa.String(length=8), nullable=False),
        sa.Column("opacity", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "telegram_user_id", "name", name="uq_watermark_presets_user_name"
        ),
    )
    op.create_index(
        "ix_watermark_presets_telegram_user_id",
        "watermark_presets",
        ["telegram_user_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_watermark_presets_telegram_user_id", table_name="watermark_presets")
    op.drop_table("watermark_presets")

"""add memory snapshots and confirmed facts

Revision ID: 21b6046db874
Revises: b75746fad074
Create Date: 2026-09-17
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "21b6046db874"
down_revision: Union[str, Sequence[str], None] = "b75746fad074"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "conversation_facts",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("conversation_id", sa.BigInteger(), nullable=False),
        sa.Column("fact_key", sa.String(length=64), nullable=False),
        sa.Column("fact_value", sa.String(length=500), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("conversation_id", "fact_key", name="uq_conversation_facts_key"),
    )
    op.create_index(
        op.f("ix_conversation_facts_conversation_id"),
        "conversation_facts",
        ["conversation_id"],
        unique=False,
    )
    op.create_table(
        "memory_snapshots",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("conversation_id", sa.BigInteger(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("brief", sa.Text(), nullable=False),
        sa.Column("covered_message_id", sa.BigInteger(), nullable=False),
        sa.Column("input_chars", sa.Integer(), nullable=False),
        sa.Column("output_chars", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "conversation_id",
            "version",
            name="uq_memory_snapshots_conversation_version",
        ),
    )
    op.create_index(
        op.f("ix_memory_snapshots_conversation_id"),
        "memory_snapshots",
        ["conversation_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_memory_snapshots_conversation_id"), table_name="memory_snapshots")
    op.drop_table("memory_snapshots")
    op.drop_index(op.f("ix_conversation_facts_conversation_id"), table_name="conversation_facts")
    op.drop_table("conversation_facts")

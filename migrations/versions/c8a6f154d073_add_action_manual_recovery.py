"""add audited manual action recovery

Revision ID: c8a6f154d073
Revises: 93f7c1b204ae
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c8a6f154d073"
down_revision: Union[str, Sequence[str], None] = "93f7c1b204ae"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "action_tasks",
        sa.Column("retry_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.execute("UPDATE action_tasks SET retry_attempts = attempts")
    op.create_table(
        "action_task_operations",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("task_id", sa.String(36), nullable=False),
        sa.Column("operator_id", sa.String(64), nullable=False),
        sa.Column("operation", sa.String(20), nullable=False),
        sa.Column("before_status", sa.String(20), nullable=False),
        sa.Column("after_status", sa.String(20), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("attempts_before", sa.Integer(), nullable=False),
        sa.Column("generation_before", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["task_id"], ["action_tasks.id"], ondelete="RESTRICT"),
    )
    op.create_index("ix_action_task_operations_task_id", "action_task_operations", ["task_id"])


def downgrade() -> None:
    op.drop_table("action_task_operations")
    op.drop_column("action_tasks", "retry_attempts")

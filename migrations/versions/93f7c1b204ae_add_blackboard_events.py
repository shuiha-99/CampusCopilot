"""add append-only blackboard events

Revision ID: 93f7c1b204ae
Revises: 7a4e5cb1d219
"""

from alembic import op
import sqlalchemy as sa


revision = "93f7c1b204ae"
down_revision = "7a4e5cb1d219"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "blackboard_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("agent", sa.String(32), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["run_id"], ["conversation_runs.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("run_id", "sequence", name="uq_blackboard_event_sequence"),
    )
    op.create_index("ix_blackboard_events_run_id", "blackboard_events", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_blackboard_events_run_id", table_name="blackboard_events")
    op.drop_table("blackboard_events")

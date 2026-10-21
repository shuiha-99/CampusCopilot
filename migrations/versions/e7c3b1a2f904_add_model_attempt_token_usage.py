"""add provider-reported token usage to model attempts

Revision ID: e7c3b1a2f904
Revises: c8a6f154d073
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e7c3b1a2f904"
down_revision: Union[str, Sequence[str], None] = "c8a6f154d073"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("model_attempts", sa.Column("prompt_tokens", sa.Integer(), nullable=True))
    op.add_column("model_attempts", sa.Column("completion_tokens", sa.Integer(), nullable=True))
    op.add_column("model_attempts", sa.Column("total_tokens", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("model_attempts", "total_tokens")
    op.drop_column("model_attempts", "completion_tokens")
    op.drop_column("model_attempts", "prompt_tokens")

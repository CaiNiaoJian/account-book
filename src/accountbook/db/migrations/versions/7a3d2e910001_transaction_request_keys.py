"""Persist request keys so a lost create response cannot cause duplicate bookkeeping.

Only adds a table; existing records and balances are untouched.
"""
import sqlalchemy as sa
from alembic import op

revision = '7a3d2e910001'
down_revision = '6b308efaafa3'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('transaction_request_keys',
        sa.Column('key', sa.String(128), primary_key=True),
        sa.Column('payload_hash', sa.String(64), nullable=False),
        sa.Column('transaction_id', sa.Integer(), sa.ForeignKey('transactions.id'), nullable=False))


def downgrade():
    op.drop_table('transaction_request_keys')

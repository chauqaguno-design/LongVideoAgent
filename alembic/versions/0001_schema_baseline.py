"""Create production accounts."""
from alembic import op
import sqlalchemy as sa

revision = "0001_schema_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('users',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('username', sa.String(length=32), nullable=False),
    sa.Column('password', sa.String(length=255), nullable=False),
    sa.Column('nickname', sa.String(length=50), nullable=False),
    sa.Column('avatar', sa.String(length=512), nullable=True),
    sa.Column('role', sa.String(length=32), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('username')
    )


def downgrade():
    op.drop_table("users")

"""VMware pending snapshot delete

Revision ID: ae2a519c8b9a
Revises: dc9ffe67a56f
Create Date: 2022-09-22 09:21:29.691045+00:00

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'ae2a519c8b9a'
down_revision = 'dc9ffe67a56f'
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table('storage_vmwarependingsnapshotdelete',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('vmware', sa.Text(), nullable=False),
    sa.Column('vm_uuid', sa.String(length=200), nullable=False),
    sa.Column('snapshot_name', sa.String(length=200), nullable=False),
    sa.Column('datetime', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['vmware_id'], ['storage_vmwareplugin.id'], name=op.f('fk_storage_vmwarependingsnapshotdelete_vmware_id_storage_vmwareplugin'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_storage_vmwarependingsnapshotdelete')),
    sqlite_autoincrement=True
    )
    with op.batch_alter_table('storage_task', schema=None) as batch_op:
        batch_op.add_column(sa.Column('task_state', sa.Text(), nullable=False, server_default='{}'))

    with op.batch_alter_table('storage_vmwareplugin', schema=None) as batch_op:
        batch_op.add_column(sa.Column('state', sa.TEXT(), nullable=False, server_default='{"state": "PENDING"}'))

    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table('storage_vmwareplugin', schema=None) as batch_op:
        batch_op.drop_column('state')

    with op.batch_alter_table('storage_task', schema=None) as batch_op:
        batch_op.drop_column('task_state')

    op.drop_table('storage_vmwarependingsnapshotdelete')
    # ### end Alembic commands ###

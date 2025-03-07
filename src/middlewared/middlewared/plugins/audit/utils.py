import middlewared.sqlalchemy as sa
import os

from sqlalchemy import Table
from sqlalchemy.ext.declarative import declarative_base

AUDIT_DATASET_PATH = '/audit'
AUDITED_SERVICES = [('MIDDLEWARE', 0.1), ('SMB', 0.1)]
AUDIT_TABLE_PREFIX = 'audit_'
AUDIT_LIFETIME = 7
AUDIT_DEFAULT_RESERVATION = 0
AUDIT_DEFAULT_QUOTA = 0
AUDIT_DEFAULT_FILL_CRITICAL = 95
AUDIT_DEFAULT_FILL_WARNING = 80
AUDIT_REPORTS_DIR = os.path.join(AUDIT_DATASET_PATH, 'reports')

AuditBase = declarative_base()


def audit_file_path(svc):
    return f'{AUDIT_DATASET_PATH}/{svc}.db'


def audit_table_name(svc, vers):
    return f'{AUDIT_TABLE_PREFIX}{svc}_{str(vers).replace(".", "_")}'


def generate_audit_table(svc, vers):
    """
    NOTE: any changes to audit table schemas should be typically be
    accompanied by a version bump for the audited service and update
    to the guiding design document for structured auditing NEP-041
    and related documents. This will potentially entail changes to
    audit-related code in the above AUDIT_SERVICES independent of the
    middleware auditing backend.

    Currently the sa.DateTime() does not give us fractional second
    precision, but for the purpose of our query interfaces, this
    should be sufficient to figure out when events happened.
    """
    return Table(
        audit_table_name(svc, vers),
        AuditBase.metadata,
        sa.Column('audit_id', sa.String(36)),
        sa.Column('message_timestamp', sa.Integer()),
        sa.Column('timestamp', sa.DateTime()),
        sa.Column('address', sa.String()),
        sa.Column('username', sa.String()),
        sa.Column('session', sa.String()),
        sa.Column('service', sa.String()),
        sa.Column('service_data', sa.JSON(dict), nullable=True),
        sa.Column('event', sa.String()),
        sa.Column('event_data', sa.JSON(dict), nullable=True),
        sa.Column('success', sa.Boolean())
    )


AUDIT_TABLES = {svc[0]: generate_audit_table(*svc) for svc in AUDITED_SERVICES}

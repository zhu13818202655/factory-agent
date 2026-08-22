from __future__ import annotations

from sqlalchemy import Column, DateTime, Integer, MetaData, String, Table
from sqlalchemy.dialects.postgresql import JSONB

metadata = MetaData()

seed_state = Table(
    "seed_state",
    metadata,
    Column("scenario", String(32), primary_key=True),
    Column("seed", Integer, nullable=False),
    Column("virtual_now", DateTime(timezone=True), nullable=False),
    Column("dataset_hash", String(64), nullable=False),
)

identity_membership = Table(
    "identity_membership",
    metadata,
    Column("membership_id", String(128), primary_key=True),
    Column("subject_id", String(128), nullable=False, index=True),
    Column("tenant_id", String(128), nullable=False, index=True),
    Column("employee_id", String(128), nullable=False),
    Column("payload", JSONB, nullable=False),
)

canonical_resource = Table(
    "canonical_resource",
    metadata,
    Column("resource_type", String(64), primary_key=True),
    Column("resource_id", String(128), primary_key=True),
    Column("tenant_id", String(128), nullable=False, index=True),
    Column("occurred_at", DateTime(timezone=True), nullable=True, index=True),
    Column("payload", JSONB, nullable=False),
)

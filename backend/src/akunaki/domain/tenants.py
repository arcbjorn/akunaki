"""Tenant constants shared across the platform.

The **system tenant** is a reserved, non-user tenant that owns platform-scoped
rows — chiefly system-wide periodic jobs (the reconciliation sweep) whose
``jobs.tenant_id`` FK must reference a real tenant even though the work is not
attributable to any single customer. It is seeded by a migration, so every
deployment has exactly one and it can never collide with a real tenant id.

Nothing user-facing is ever attributed to this tenant; it is an internal owner
for system jobs only. Its fixed, non-UUID id makes that intent unmistakable and
lets code reference it without a lookup.
"""

from __future__ import annotations

# Reserved id for the platform's own (non-user) tenant. Fixed, not a UUID, so it
# is visibly a system row and cannot clash with an allocated tenant id.
SYSTEM_TENANT_ID = "system"

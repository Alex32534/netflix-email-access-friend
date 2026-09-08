"""Legacy data migration boundary.

The public application no longer imports user or assignment JSON data. Existing
SQLite tables are intentionally retained by the schema so old volumes remain
readable, but legacy account data is not activated by the new search flow.
"""


def migrate_old_json_if_present():
    return {"migrated_users": 0, "migrated_emails": 0, "skipped": True}

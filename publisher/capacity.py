"""Admission reservations, shared by quota checks and user-facing usage."""
import json

from publisher.store import ARTIFACT_DRIVER, SANDBOX_DRIVER, NATIVE_DATABASE_DRIVER

ACCOUNT = {"memory_mib": 4096, "vcpus": 4}
PLATFORM = {"memory_mib": 20480, "vcpus": 6}
SANDBOX = {"memory_mib": 2048, "vcpus": 1.5}
DATABASE = {"memory_mib": 256, "vcpus": 0.25}
LEGACY_VM = {"memory_mib": 6144, "vcpus": 2}
ACTIVE = ("uploading", "queued", "building", "migrating", "deploying", "activating", "cancelling")


def empty():
    return {"memory_mib": 0, "vcpus": 0}


def add(total, amount):
    for key in total:
        total[key] = round(total[key] + amount[key], 6)


def release(row):
    if row["runtime_driver"] == ARTIFACT_DRIVER or (
            row["state"] in {"failed", "cancelled"} and row["backend_id"] is None):
        return empty()
    config = json.loads(row["config"])
    return {"memory_mib": config["memory_mb"], "vcpus": config["cpus"]}


def reservations(db, excluding="", *, legacy_vm_accounting=False):
    result = {"platform": empty(), "sandbox": empty(), "accounts": {}}
    legacy_accounts = set()
    rows = db.execute("SELECT d.*,p.account_id FROM deployments d JOIN projects p ON p.id=d.project_id WHERE d.id!=?",
                      (excluding,)).fetchall()
    for row in rows:
        amount = release(row)
        if legacy_vm_accounting and row['runtime_driver'] is None and any(amount.values()):
            legacy_accounts.add(row['account_id'])
        else:
            add(result["platform"], amount)
        add(result["accounts"].setdefault(row["account_id"], empty()), amount)
        if row["runtime_driver"] == SANDBOX_DRIVER:
            add(result["sandbox"], amount)
    for row in db.execute("SELECT p.account_id,r.runtime_driver,r.kind FROM data_resources r JOIN projects p ON p.id=r.project_id"):
        if row['kind'] != 'database':
            if legacy_vm_accounting and row['runtime_driver'] is None:
                legacy_accounts.add(row['account_id'])
            continue
        if legacy_vm_accounting and row['runtime_driver'] is None:
            legacy_accounts.add(row['account_id'])
        else:
            add(result["platform"], DATABASE)
        add(result["accounts"].setdefault(row["account_id"], empty()), DATABASE)
        if row['runtime_driver'] == NATIVE_DATABASE_DRIVER:
            add(result['sandbox'], DATABASE)
    if legacy_vm_accounting:
        legacy_accounts.update(row['account_id'] for row in db.execute('SELECT account_id FROM legacy_vm_reservations'))
        for account in legacy_accounts:
            add(result['platform'], LEGACY_VM)
        result['legacy_accounts'] = sorted(legacy_accounts)
    return result


def platform_increment(totals, account, config, driver, *, legacy_vm_accounting=False):
    if legacy_vm_accounting and driver is None:
        return empty() if account in totals['legacy_accounts'] else LEGACY_VM.copy()
    return {'memory_mib': config['memory_mb'], 'vcpus': config['cpus']}


def active_count(db, account):
    return db.execute("SELECT count(*) FROM deployments d JOIN projects p ON p.id=d.project_id "
                      "WHERE p.account_id=? AND d.state IN (" + ",".join("?" for _ in ACTIVE) + ")",
                      (account, *ACTIVE)).fetchone()[0]

"""Durable reconciliation, with per-provider baselines and write intentions.

Never treat a failed read as an empty list. Every write is persisted as an
intention before execution. Ambiguous outcomes are held for review instead of
blindly retrying creates and producing duplicates.
"""

import copy
import hashlib
import json
import unicodedata
import uuid
from collections import defaultdict

from listenbruecke.models import ConnectorError, Item


def fingerprint(fields):
    def normalize(value):
        return " ".join(unicodedata.normalize("NFKC", value).casefold().split())
    return (normalize(fields["name"]), normalize(fields.get("description", "")))


class SyncEngine:
    def __init__(self, store):
        self.store = store

    def state_key(self, targets):
        keys = [f"{t['provider']}:{t['list_id']}" for t in targets]
        group = hashlib.sha256(json.dumps({
            "targets": sorted(keys), "generation": self.store.get("account_generation", 0)
        }, sort_keys=True).encode()).hexdigest()[:24]
        return "sync:" + group

    def include_target(self, targets, target):
        """Retain every baseline/intention while extending an existing group."""
        destination = self.state_key([*targets, target])
        if self.store.get(destination) is None:
            original = self.store.get(self.state_key(targets))
            if original is not None:
                self.store.set(destination, copy.deepcopy(original))

    async def run(self, targets, connectors, *, propagate_deletions=False):
        if len(targets) < 2:
            raise ConnectorError("Bitte mindestens zwei Listen für den Abgleich auswählen.")
        keys = {f"{t['provider']}:{t['list_id']}": t for t in targets}
        if len(keys) != len(targets):
            raise ConnectorError("Jede Liste darf nur einmal ausgewählt werden.")
        snapshots = {}
        # Complete all reads before considering a single mutation.
        for key, target in keys.items():
            items = await connectors[target["provider"]].get_items(target["list_id"])
            if len({item.uid for item in items}) != len(items):
                raise ConnectorError("Eine Liste enthält mehrdeutige Artikel-IDs. Der Abgleich wurde angehalten.")
            snapshots[key] = {item.uid: item for item in items}
        # Bring lists exposed through HA must never be synchronized a second time.
        for key, data in snapshots.items():
            if key.startswith("bring:") and data:
                for other, other_data in snapshots.items():
                    if other.startswith("homeassistant:") and set(data) & set(other_data):
                        raise ConnectorError("Diese Home-Assistant-Liste ist bereits dieselbe Bring!-Liste. Bitte nur eine davon auswählen.")
        state_key = self.state_key(targets)
        state = self.store.get(state_key, {"records": {}, "ignored": {}})
        records = state["records"]
        counts = {"ok": True, "added": 0, "updated": 0, "removed": 0, "conflicts": 0}

        def save():
            self.store.set(state_key, state)

        def conflict(message):
            counts["conflicts"] += 1
            self.store.event("warning", message)

        # Recover outcomes of previously interrupted creates before matching new items.
        for record in records.values():
            for key, pending in list(record.get("creating", {}).items()):
                candidates = [item for uid, item in snapshots[key].items()
                              if uid not in pending["before"] and fingerprint(item.fields()) == fingerprint(pending["fields"])
                              and item.completed == pending["fields"]["completed"]]
                if len(candidates) == 1:
                    item = candidates[0]
                    record["refs"][key] = item.uid
                    record["views"][key] = item.fields()
                    record.get("dirty", {}).pop(key, None)
                    del record["creating"][key]
                    save()
                else:
                    conflict("Ein unbestätigter Schreibvorgang ist offen. Bitte die betroffene Liste prüfen; es werden keine doppelten Artikel angelegt.")
            for key, pending in list(record.get("updating", {}).items()):
                if pending["uid"] in snapshots[key]:
                    # Updates of an existing ID are idempotent. Reconcile its
                    # fresh fields below, including edits made after interruption.
                    del record["updating"][key]
                    save()
                    continue
                # A Bring rename can replace the native ID before a connection
                # fails. Adopt exactly one new matching ID; never copy it again.
                candidates = [item for uid, item in snapshots[key].items()
                              if uid not in pending["before"]
                              and fingerprint(item.fields()) == fingerprint(pending["fields"])
                              and item.completed == pending["fields"]["completed"]]
                if len(candidates) == 1:
                    item = candidates[0]
                    record["refs"][key] = item.uid
                    record["views"][key] = item.fields()
                    record.get("dirty", {}).pop(key, None)
                    del record["updating"][key]
                    save()
                else:
                    conflict("Eine unbestätigte Artikeländerung ist offen. Bitte die betroffene Liste prüfen; es werden keine doppelten Artikel angelegt.")

        known = {key: set() for key in keys}
        for record in records.values():
            for key, uid in record["refs"].items():
                if key in known:
                    known[key].add(uid)
        pending_keys = {key for rec in records.values()
                        for operation in ("creating", "updating") for key in rec.get(operation, {})}
        groups = defaultdict(lambda: defaultdict(list))
        for key, data in snapshots.items():
            ignored = state["ignored"].setdefault(key, [])
            for uid, item in data.items():
                if uid in known[key] or key in pending_keys:
                    continue
                if item.completed:
                    if uid not in ignored:
                        ignored.append(uid)
                    continue  # Historical purchases aren't new shopping requirements.
                if uid in ignored:
                    ignored.remove(uid)  # A user has explicitly reopened an old item.
                if item.name.strip():
                    groups[fingerprint(item.fields())][key].append(item)
        for per_key in groups.values():
            for items in per_key.values():
                items.sort(key=lambda item: item.uid)
            # Match at most one occurrence from each provider. Preserve duplicates
            # inside a provider, even when ingredients share a name and amount.
            for index in range(max(map(len, per_key.values()))):
                existing = {key: items[index] for key, items in per_key.items() if index < len(items)}
                first = next(iter(existing.values()))
                records[str(uuid.uuid4())] = {
                    "fields": first.fields(), "refs": {key: item.uid for key, item in existing.items()},
                    "views": {key: item.fields() for key, item in existing.items()},
                    "dirty": {}, "suppressed": [], "creating": {}, "retired": False,
                }
        save()

        for record in records.values():
            if record.get("retired") or record.get("creating") or record.get("updating"):
                continue
            current = {key: snapshots[key].get(uid) for key, uid in record["refs"].items()}
            missing = [key for key, item in current.items() if item is None and key not in record["suppressed"]]
            changes = defaultdict(list)
            pending_conflicts = set()
            for key, item in current.items():
                if item is None:
                    continue
                baseline = record["views"].get(key, item.fields())
                for field, value in item.fields().items():
                    if baseline.get(field) != value:
                        changes[field].append(value)
                        if (field in record.get("dirty", {}).get(key, [])
                                and value != record["fields"].get(field)):
                            pending_conflicts.add(field)
            if any(len({item.fields()[field] for item in current.values() if item}) > 1
                   for field in pending_conflicts):
                conflict("Ein Artikel wurde während eines unbestätigten Schreibvorgangs geändert. Bitte die Angaben in den Listen angleichen.")
                continue
            if any(any(value != values[0] for value in values[1:]) for values in changes.values()):
                conflict("Ein Artikel wurde gleichzeitig unterschiedlich geändert. Bitte die Angaben in den Listen angleichen.")
                continue
            if missing and propagate_deletions:
                if changes or any(item and not item.deletable for item in current.values()):
                    conflict("Eine Löschung kollidiert mit einer Änderung oder einem geschützten Rezeptbestandteil. Die übrigen Artikel bleiben erhalten.")
                    continue
                for key, item in current.items():
                    if item is not None:
                        target = keys[key]
                        await connectors[target["provider"]].delete_item(target["list_id"], item)
                        snapshots[key].pop(item.uid, None)
                        counts["removed"] += 1
                record["retired"] = True
                save()
                continue
            if missing:
                record["suppressed"].extend(missing)
                save()  # Never resurrect an explicitly removed item.

            changed_fields = set(changes)
            if changed_fields & {"name", "description"} and any(item and not item.editable for item in current.values()):
                conflict("Name oder Menge eines Monsieur-Cuisine-Rezeptbestandteils kann nicht zurückgeschrieben werden. Die Änderung bleibt zur Prüfung offen.")
                continue
            if changed_fields:
                for field, values in changes.items():
                    record["fields"][field] = values[0]
                for key in keys:
                    record["dirty"][key] = sorted(set(record["dirty"].get(key, [])) | changed_fields)
                for key, item in current.items():
                    if item:
                        record["views"][key] = item.fields()
                save()

            for key, target in keys.items():
                if key in record["suppressed"] or key in pending_keys:
                    continue
                connector = connectors[target["provider"]]
                uid = record["refs"].get(key)
                item = snapshots[key].get(uid) if uid else None
                if uid and item is None:
                    continue
                if item is None:
                    fields = record["fields"]
                    record["creating"][key] = {"before": list(snapshots[key]), "fields": dict(fields)}
                    save()
                    item = await connector.add_item(target["list_id"], **fields)
                    record["refs"][key] = item.uid
                    record["views"][key] = item.fields()
                    record["creating"].pop(key, None)
                    record["dirty"].pop(key, None)
                    snapshots[key][item.uid] = item
                    counts["added"] += 1
                    save()
                elif record["dirty"].get(key):
                    fields = record["dirty"][key]
                    values = {field: record["fields"][field] for field in fields}
                    if all(item.fields().get(field) == value for field, value in values.items()):
                        record["dirty"].pop(key, None)
                        record["views"][key] = item.fields()
                        save()
                        continue
                    record.setdefault("updating", {})[key] = {
                        "uid": item.uid, "before": list(snapshots[key]),
                        "fields": {**item.fields(), **values},
                    }
                    save()
                    updated = await connector.update_item(target["list_id"], item, **values)
                    # Bring renames can replace the native UUID.
                    record["refs"][key] = updated.uid
                    record["views"][key] = updated.fields()
                    record["dirty"].pop(key, None)
                    record["updating"].pop(key, None)
                    snapshots[key].pop(item.uid, None)
                    snapshots[key][updated.uid] = updated
                    counts["updated"] += 1
                    save()
        counts["ok"] = counts["conflicts"] == 0
        counts["message"] = ("Alle ausgewählten Listen sind abgeglichen." if counts["ok"]
                             else "Abgleich abgeschlossen; offene Konflikte bitte im Protokoll prüfen.")
        return counts

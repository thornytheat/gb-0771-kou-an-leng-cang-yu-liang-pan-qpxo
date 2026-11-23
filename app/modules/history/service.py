from datetime import datetime, timezone
from time import perf_counter_ns
from app.faults import DockFault
from app.pipeline import review
from app.seals import compact, input_seals, sha
from app.settings import VERSION
from .corrections import corrected_slots, validate_change


def stamp_for(files):
    begin = perf_counter_ns()
    hashes = input_seals(files)
    key = sha(compact([VERSION, hashes]))
    stages = review(files)
    result = sha(compact(stages))
    stamp = dict(id=key, version=VERSION, inputs=hashes, result_sha256=result, window=stages['window'],
                 elapsed_us=(perf_counter_ns() - begin) // 1000,
                 generated_at=datetime.now(timezone.utc).isoformat())
    return key, stamp, stages


def save(archive, files):
    key, stamp, stages = stamp_for(files)
    result = stamp['result_sha256']
    document, reused = archive.insert(key, stamp, files, stages)
    if document['manifest']['result_sha256'] != result or sha(compact(document['stages'])) != result:
        raise DockFault('DRIFT', '归档与原件复算不一致', 409)
    return dict(brief(document), reused=reused)


def apply_correction(archive, payload):
    change = validate_change(payload)
    correction_id = sha(compact([VERSION, 'correction', change]))
    existing = archive.correction(correction_id)
    if existing:
        return summarize(archive, existing, idempotent=True)
    # 仅对现行版本（链路顶端）重算；已被更正的留档冻结，不再派生新分支
    head_ids, _ = archive.lineage_heads()
    linked, present = [], False
    for head in head_ids:
        document = archive.read(head)
        try:
            new_slots, matched, changed = corrected_slots(document['files']['slots'], change)
        except DockFault as exc:
            if exc.code == 'CORRECTION_TARGET':
                continue
            raise
        present = present or matched > 0
        if not changed:
            continue
        files = dict(document['files'], slots=new_slots)
        # Recompute every linked shift before opening the transaction so a failure
        # leaves the whole batch on its previous versions.
        new_key, stamp, stages = stamp_for(files)
        if sha(compact(stages)) != stamp['result_sha256']:
            raise DockFault('DRIFT', '归档与原件复算不一致', 409)
        linked.append((document, new_key, stamp, files, stages))
    if not present:
        raise DockFault('CORRECTION_TARGET', '归档中未找到待更正的槽位版本')
    if not linked:
        return dict(id=correction_id, change=change, status='noop', idempotent=True, receipts=[])
    now = datetime.now(timezone.utc).isoformat()
    batch = [(document, new_key, stamp, files, stages, new_key == document['manifest']['id'])
             for document, new_key, stamp, files, stages in linked]
    created = archive.apply_correction(correction_id, change, batch, now)
    return summarize(archive, archive.correction(correction_id), idempotent=not created)


def summarize(archive, record, idempotent):
    receipts = []
    for item in record['detail']:
        document = archive.read(item['result'])
        receipts.append(dict(source=item['source'], reused=item['reused'], **brief(document)))
    return dict(id=record['id'], change={key: record[key] for key in ('slot', 'revision', 'door')},
                status=record['status'], idempotent=idempotent, applied_at=record['applied_at'], receipts=receipts)


def brief(document):
    stamp, stages = document['manifest'], document['stages']
    return dict(id=stamp['id'], manifest=stamp, capacity=stages['capacity']['counts'], exposure=stages['exposure']['counts'])

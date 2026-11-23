import csv
from io import StringIO
from app.faults import DockFault
from app.modules.intake.contracts import MISSING

FIELDS = ('slot', 'revision', 'door', 'capacity', 'from', 'until')


def validate_change(change):
    if not isinstance(change, dict) or set(change) != {'slot', 'revision', 'door'}:
        raise DockFault('CORRECTION_FIELDS', '门区更正须含槽位、版本和更正门区')
    if any(not isinstance(change[key], str) or change[key].strip() in MISSING for key in FIELDS[:3]):
        raise DockFault('CORRECTION_FIELDS', '门区更正字段缺失')
    return {key: change[key].strip() for key in FIELDS[:3]}


def corrected_slots(text, change):
    """Rewrite every matching slot revision's door, keeping other rows and physical order intact.

    Returns (new_text, matched_rows, changed_rows); changed rows are those whose door differed.
    """
    reader = csv.reader(StringIO(text.removeprefix('﻿'), newline=''), strict=True)
    output = StringIO(newline='')
    writer = csv.writer(output, lineterminator='\n')
    matched = changed = 0
    header = next(reader, None)
    if header is None or set(header) != set(FIELDS) or len(header) != len(FIELDS):
        raise DockFault('HEADER', 'slots 表头不符')
    writer.writerow(header)
    index = {name: header.index(name) for name in FIELDS}
    for raw in reader:
        if not raw:
            writer.writerow(raw)
            continue
        row = list(raw)
        if len(row) == len(header) and row[index['slot']].strip() == change['slot'] \
                and row[index['revision']].strip() == change['revision']:
            matched += 1
            if row[index['door']].strip() != change['door']:
                row[index['door']] = change['door']
                changed += 1
        writer.writerow(row)
    if not matched:
        raise DockFault('CORRECTION_TARGET', '归档中未找到待更正的槽位版本')
    return output.getvalue(), matched, changed

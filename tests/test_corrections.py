import pytest
from concurrent.futures import ThreadPoolExecutor
from app.faults import DockFault
from app.main import build
from app.modules.history.corrections import corrected_slots, validate_change
from app.modules.history.service import apply_correction
from app.parcels import load_example
from app.pipeline import review

CHANGE = dict(slot='A2', revision='01', door='WEST')


def shift_pack(sample, window, suffix=''):
    pack = dict(load_example(sample))
    pack['window'] = window
    if suffix:
        pack['slots'] += suffix
    return pack


def archived_pack(client, pack):
    return client.post('/api/receipts', json=pack).json


@pytest.fixture
def two_shifts(client):
    extra = 'A3,01,EAST,6,2026-11-21T00:00:00Z,2026-11-21T01:00:00Z\n'
    first = archived_pack(client, shift_pack(
        'dirty', '{"from":"2026-11-21T08:00:00+08:00","until":"2026-11-21T08:30:00+08:00"}', extra))
    second = archived_pack(client, shift_pack(
        'dirty', '{"from":"2026-11-21T08:30:00+08:00","until":"2026-11-21T09:00:00+08:00"}'))
    clean_pack = shift_pack('clean', '{"from":"2026-11-21T08:00:00+08:00","until":"2026-11-21T09:00:00+08:00"}')
    clean_pack['slots'] = clean_pack['slots'].replace('A2,01,EAST', 'A2,03,EAST').replace('A2,02,WEST', 'A2,04,WEST')
    clean = archived_pack(client, clean_pack)
    return first, second, clean


def test_corrected_slots_keeps_other_rows_and_line_endings():
    text = load_example('dirty')['slots']
    new_text, matched, changed = corrected_slots(text, CHANGE)
    assert matched == changed == 1 and new_text.count('\n') == text.count('\n')
    lines = [line for line in new_text.splitlines() if line.startswith('A2,01')]
    assert lines == ['A2,01,WEST,8,2026-11-21T00:00:00Z,2026-11-21T00:40:00Z']
    untouched = [line for line in new_text.splitlines() if line.startswith('A2,02')]
    assert untouched and untouched[0].startswith('A2,02,WEST')


def test_unknown_target_raises():
    with pytest.raises(DockFault) as error:
        corrected_slots(load_example('dirty')['slots'], dict(slot='Z9', revision='01', door='WEST'))
    assert error.value.code == 'CORRECTION_TARGET'


def test_validate_change_rejects_missing_door():
    with pytest.raises(DockFault) as error:
        validate_change(dict(slot='A2', revision='01', door='   '))
    assert error.value.code == 'CORRECTION_FIELDS'


def test_correction_republishes_every_linked_shift(client, two_shifts):
    first, second, clean = two_shifts
    result = client.post('/api/receipts/corrections', json=CHANGE).json
    assert result['status'] == 'applied' and result['idempotent'] is False
    assert len(result['receipts']) == 2
    new_ids = {item['id'] for item in result['receipts']}
    assert first['id'] not in new_ids and second['id'] not in new_ids
    # 无关班次（净样例）原样保留，不在更正批次内
    assert client.get(f"/api/receipts/{clean['id']}").status_code == 200
    listing = client.get('/api/receipts').json
    assert listing['count'] == 5
    superseded = {row['id']: row for row in listing['rows'] if row['superseded']}
    assert set(superseded) == {first['id'], second['id']}


def test_corrected_share_uses_new_door_for_overtemperature(client, two_shifts):
    _, second, _ = two_shifts
    before = client.get(f"/api/receipts/{second['id']}").json
    assert 900 == before['exposure']['warm']
    result = client.post('/api/receipts/corrections', json=CHANGE).json
    corrected = next(item for item in result['receipts'] if item['manifest']['window'] == before['manifest']['window'])
    stages = client.get('/api/observe/registry', query_string={'receipt': corrected['id']}).json
    a2_old = next(row for row in stages['versions']['A2'] if row['revision'] == '01')
    assert a2_old['door'] == 'WEST'
    # 门区结论随之改变：A2/01 段从 EAST 门态切到 WEST 门态，跨班超温分钟重算
    exposure = client.get('/api/observe/exposure', query_string={'receipt': corrected['id']}).json
    doors = {row['door'] for row in exposure['rows'] if row['slot'] == 'A2'}
    assert doors == {'WEST'}
    assert exposure['counts']['warm'] == 1200


def test_old_versions_remain_readable_for_verification(client, two_shifts):
    first, second, _ = two_shifts
    client.post('/api/receipts/corrections', json=CHANGE)
    old_first = client.get(f"/api/receipts/{first['id']}").json
    old_second = client.get(f"/api/receipts/{second['id']}").json
    assert old_first['manifest']['id'] == first['id']
    assert old_second['manifest']['id'] == second['id']
    detail = client.get(f"/api/receipts/{first['id']}").json
    assert detail['versions'][0] == first['id'] and len(detail['versions']) == 2


def test_repeating_same_correction_creates_no_new_version(client, two_shifts):
    first, second, _ = two_shifts
    once = client.post('/api/receipts/corrections', json=CHANGE).json
    again = client.post('/api/receipts/corrections', json=CHANGE).json
    assert again['id'] == once['id'] and again['idempotent'] is True
    assert {item['id'] for item in again['receipts']} == {item['id'] for item in once['receipts']}
    assert client.get('/api/receipts').json['count'] == 5
    assert client.get('/api/receipts/corrections').json['count'] == 1


def test_failed_recompute_keeps_all_linked_versions(client, two_shifts, monkeypatch):
    first, second, _ = two_shifts
    # 复算流水线在第二个关联班次处失败：整批在事务前中断，旧版全部保留供查证
    from app.modules.history import service as history_service
    calls = {'n': 0}

    def broken(files):
        calls['n'] += 1
        if calls['n'] == 2:
            raise DockFault('RECOMPUTE', '跨班次重算失败', 422)
        return review(files)

    monkeypatch.setattr(history_service, 'review', broken)
    response = client.post('/api/receipts/corrections', json=CHANGE)
    assert (response.status_code, response.json['code']) == (422, 'RECOMPUTE')
    assert client.get('/api/receipts').json['count'] == 3
    assert client.get('/api/receipts/corrections').json['count'] == 0
    # 旧版仍可查证，且无链路
    detail = client.get(f"/api/receipts/{first['id']}").json
    assert detail['versions'] == [first['id']]
    assert client.get(f"/api/receipts/{second['id']}").json['manifest']['id'] == second['id']


def test_unknown_target_across_archive(client, two_shifts):
    response = client.post('/api/receipts/corrections',
                           json=dict(slot='Z9', revision='01', door='WEST'))
    assert (response.status_code, response.json['code']) == (422, 'CORRECTION_TARGET')


def test_noop_when_door_already_corrected(client, two_shifts):
    client.post('/api/receipts/corrections', json=CHANGE)
    result = client.post('/api/receipts/corrections', json=dict(slot='A2', revision='02', door='WEST')).json
    assert result['status'] == 'noop' and result['receipts'] == []


def test_chained_correction_extends_only_current_heads(client, two_shifts):
    first, second, _ = two_shifts
    first_new = client.post('/api/receipts/corrections', json=CHANGE).json
    # 第二次更正只命中首个班次独有的 A3；旧留档不再派生分支
    follow_up = client.post('/api/receipts/corrections',
                            json=dict(slot='A3', revision='01', door='NORTH')).json
    assert follow_up['status'] == 'applied' and len(follow_up['receipts']) == 1
    newest = follow_up['receipts'][0]['id']
    chain = client.get(f"/api/receipts/{newest}").json['versions']
    assert chain[0] == first['id'] and chain[-1] == newest and len(chain) == 3
    # 第二个班次的链路仍是两级
    second_head = next(item['id'] for item in first_new['receipts'] if item['source'] == second['id'])
    assert len(client.get(f"/api/receipts/{second_head}").json['versions']) == 2
    listing = client.get('/api/receipts').json
    # 两个关联班次的最新版 + 未受影响的净样例班次，共三个现行版本
    assert sum(row['current'] for row in listing['rows']) == 3
    assert sum(row['superseded'] for row in listing['rows']) == 3
    assert client.get('/api/receipts/corrections').json['count'] == 2


def test_history_page_shows_correction_and_lineage(client, two_shifts):
    first, _, _ = two_shifts
    client.post('/api/receipts/corrections', json=CHANGE)
    html = client.get('/view/history', query_string={'receipt': first['id']}).get_data(as_text=True)
    assert '槽位挂错门 · 跨班次更正' in html and '已被更正 · 留档查证' in html
    assert '本档版本链路' in html and '重算关联班次' in html


def test_concurrent_identical_correction(tmp_path):
    from app.modules.history.service import save
    store = build(tmp_path / 'dock.sqlite3').extensions['ice_receipts']
    extra = 'A3,01,EAST,6,2026-11-21T00:00:00Z,2026-11-21T01:00:00Z\n'
    save(store, shift_pack('dirty', '{"from":"2026-11-21T08:00:00+08:00","until":"2026-11-21T08:30:00+08:00"}', extra))
    save(store, shift_pack('dirty', '{"from":"2026-11-21T08:30:00+08:00","until":"2026-11-21T09:00:00+08:00"}'))
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: apply_correction(store, CHANGE), range(4)))
    assert len({item['id'] for item in results}) == 1
    assert sum(not item['idempotent'] for item in results) == 1
    assert len(store.list_corrections()) == 1

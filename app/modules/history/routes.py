from flask import Blueprint, current_app, jsonify, request
from app.parcels import receive_pack
from .service import apply_correction, brief, save

history = Blueprint('history', __name__, url_prefix='/api/receipts')


def receipts():
    return current_app.extensions['ice_receipts']


@history.get('')
def listing():
    rows = receipts().list()
    heads, superseded = receipts().lineage_heads()
    for row in rows:
        row['superseded'] = row['id'] in superseded
        row['current'] = row['id'] in heads
    return jsonify(rows=rows, count=len(rows))


@history.post('')
def publish():
    return jsonify(save(receipts(), receive_pack(request.get_json(silent=True))))


@history.get('/corrections')
def correction_log():
    rows = receipts().list_corrections()
    return jsonify(rows=rows, count=len(rows))


@history.post('/corrections')
def correct():
    return jsonify(apply_correction(receipts(), request.get_json(silent=True)))


@history.get('/<key>')
def detail(key):
    document = receipts().read(key)
    payload = brief(document)
    payload['versions'] = receipts().chain(key)
    return jsonify(payload)


@history.post('/<key>/refresh')
def refresh(key):
    return jsonify(save(receipts(), receipts().read(key)['files']))

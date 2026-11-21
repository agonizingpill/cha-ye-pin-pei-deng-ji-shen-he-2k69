from datetime import timezone

from sqlalchemy import select

from .constants import ATTRIBUTES, ReviewState
from .models import GradeEvidence, SensoryPanel, User
from .workflow import current_panel, lot_state, repanel_panel, sample_for


def iso(when):
    return when.replace(tzinfo=timezone.utc).isoformat(timespec='seconds') if when else None


def user_out(user):
    return {'id': user.id, 'username': user.username, 'name': user.name, 'role': user.role}


def panel_out(row, db):
    return {'id': row.id, 'author': db.get(User, row.author_id).name, 'grade': row.grade,
            'note': row.note, 'created_at': iso(row.created_at),
            'supersedes_id': row.supersedes_id, 'superseded_at': iso(row.superseded_at),
            'invalidated_at': iso(row.invalidated_at),
            'scores': {key: getattr(row, key) for key in ATTRIBUTES}}


def sample_out(row):
    return {'id': row.id, 'seal': row.seal, 'grams': row.grams, 'cabinet': row.cabinet,
            'lot_id': row.lot_id, 'review_id': row.review_id, 'sealed_at': iso(row.sealed_at)} if row else None


def lot_out(row, db):
    current = current_panel(db, row)
    return {'id': row.id, 'code': row.code, 'cultivar': row.cultivar, 'origin': row.origin,
            'roast': row.roast, 'kilograms': str(row.kilograms), 'received': row.received.isoformat(),
            'state': lot_state(db, row), 'current_panel_id': current.id if current else None,
            'panels': [panel_out(panel, db) for panel in reversed(row.panels)],
            'sample': sample_out(sample_for(db, lot_id=row.id))}


def evidence_out(row, db):
    return {'generation': row.generation, 'cause': row.cause, 'note': row.note,
            'snapshot': row.snapshot, 'digest': row.digest, 'result': row.result,
            'created_at': iso(row.created_at), 'superseded_at': iso(row.superseded_at)}


def basis_out(review, db):
    """Issued grades keep every frozen basis so old and new scores can be compared."""
    rows = db.scalars(select(GradeEvidence).filter_by(review_id=review.id)
                      .order_by(GradeEvidence.generation)).all()
    return [evidence_out(row, db) for row in rows]


def voided_out(review, db):
    rows = db.scalars(select(SensoryPanel).filter(
        SensoryPanel.review_id == review.id,
        SensoryPanel.invalidated_at.isnot(None)).order_by(SensoryPanel.id)).all()
    return [panel_out(row, db) for row in rows]


def latest_version(db, panel_id):
    """Follow the correction chain and return the newest version of a sensory record."""
    seen = set()
    current = db.get(SensoryPanel, panel_id)
    while current is not None and current.id not in seen:
        seen.add(current.id)
        nxt = db.scalar(select(SensoryPanel).filter(SensoryPanel.supersedes_id == current.id)
                        .order_by(SensoryPanel.id.desc()).limit(1))
        if nxt is None:
            return current
        current = nxt
    return current


def score_changes(review, db):
    """Scores that were corrected after this review froze them, old and new side by side.

    Signed grades keep their original basis, so this is how the blend page still shows
    which score has since been corrected without rewriting the issued evidence.
    """
    frozen = {item['panel_id']: item for item in review.snapshot['ingredients']}
    changes = []
    for panel_id, ingredient in frozen.items():
        latest = latest_version(db, panel_id)
        if latest is None or latest.id == panel_id:
            continue
        changes.append({'lot': ingredient['lot'], 'frozen_panel_id': panel_id, 'current_panel_id': latest.id,
                        'frozen': ingredient['scores'],
                        'current': {key: getattr(latest, key) for key in ATTRIBUTES},
                        'corrected_at': iso(latest.created_at)})
    return changes


def review_out(row, db):
    panel = repanel_panel(db, row)
    result = row.result
    if result is not None and row.state == ReviewState.REPANEL:
        result = {key: result[key] for key in ('confidence', 'effective_threshold', 'calibrated_floor',
                                                'model', 'seed', 'input_digest', 'validation')}
    return {'id': row.id, 'revision': row.revision, 'generation': row.generation,
            'state': row.state, 'failure': row.failure,
            'snapshot': row.snapshot, 'digest': row.digest, 'result': result, 'attempts': row.attempts,
            'verified_by': db.get(User, row.verified_by).name if row.verified_by else None,
            'signed_by': db.get(User, row.signed_by).name if row.signed_by else None,
            'final_grade': row.final_grade if row.signed_by else None,
            'repanel': panel_out(panel, db) if panel else None,
            'voided_repanels': voided_out(row, db) if row.generation else [],
            'score_changes': score_changes(row, db),
            'basis': basis_out(row, db),
            'sample': sample_out(sample_for(db, review_id=row.id))}


def recipe_out(row, db):
    return {'id': row.id, 'code': row.code, 'name': row.name, 'revision': row.revision, 'note': row.note,
            'author_id': row.author_id, 'author': db.get(User, row.author_id).name,
            'shares': [{'lot_id': share.lot_id, 'code': share.lot.code, 'cultivar': share.lot.cultivar,
                        'percent': str(share.percent), 'lot_state': lot_state(db, share.lot)} for share in row.shares],
            'review': review_out(row.reviews[-1], db) if row.reviews else None}


def policy_out(row, db):
    return {'id': row.id, 'floor': str(row.floor), 'reason': row.reason,
            'author': db.get(User, row.author_id).name, 'created_at': iso(row.created_at)}

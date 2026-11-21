import copy
import logging
from decimal import Decimal

from fastapi import HTTPException
from redis import RedisError
from sqlalchemy import select

from .constants import ATTRIBUTES, ReviewState as State
from .db import QUEUE, redis
from .grade_model import digest
from .models import (Audit, BlendRecipe, BlendShare, GradeEvidence, GradeReview, RetainedSample,
                     SensoryPanel, TeaLot, ThresholdPolicy, now)

logger = logging.getLogger(__name__)


def record(db, user, event, subject, explanation):
    db.add(Audit(actor=user.username if hasattr(user, 'username') else user, event=event,
                 subject=str(subject), explanation=explanation))


def require(db, model, key, lock=False):
    row = db.get(model, key, with_for_update=lock)
    if row is None:
        raise HTTPException(404, '记录不存在')
    return row


def sample_for(db, **reference):
    return db.scalar(select(RetainedSample).filter_by(**reference))


def current_panel(db, lot):
    """The lot's active sensory record. Corrections add a new version and retire the old one."""
    return db.scalar(select(SensoryPanel).filter(
        SensoryPanel.lot_id == lot.id,
        SensoryPanel.superseded_at.is_(None),
        SensoryPanel.invalidated_at.is_(None),
    ).order_by(SensoryPanel.id.desc()).limit(1))


def lot_state(db, lot):
    if current_panel(db, lot) is None:
        return 'awaiting_panel'
    if not sample_for(db, lot_id=lot.id):
        return 'awaiting_sample'
    return 'ready'


def replace_shares(db, recipe, data):
    for share in data.shares:
        require(db, TeaLot, share.lot_id)
    incoming = {share.lot_id: share.percent for share in data.shares}
    for existing in list(recipe.shares):
        if existing.lot_id in incoming:
            existing.percent = incoming.pop(existing.lot_id)
        else:
            recipe.shares.remove(existing)
    recipe.shares.extend(BlendShare(lot_id=key, percent=percent) for key, percent in incoming.items())


def latest_policy(db):
    policy = db.scalar(select(ThresholdPolicy).order_by(ThresholdPolicy.id.desc()).limit(1))
    if policy is None:
        raise HTTPException(409, '请主管先登记模型阈值版本')
    return policy


def freeze(db, recipe):
    total = sum((share.percent for share in recipe.shares), Decimal('0'))
    if total != Decimal('100.00'):
        raise HTTPException(409, f'配比为 {total:.2f}%，必须闭合至 100.00%')
    ingredients = []
    for share in sorted(recipe.shares, key=lambda row: row.lot_id):
        lot = share.lot
        # Lock ingredients in a stable order so a concurrent score correction serialises with freezing.
        require(db, TeaLot, lot.id, lock=True)
        sample = sample_for(db, lot_id=lot.id)
        if sample is None:
            raise HTTPException(409, f'{lot.code} 缺失原料留样，封签登记后再送审')
        panel = current_panel(db, lot)
        if panel is None:
            raise HTTPException(409, f'{lot.code} 缺失感官评审，请补录')
        ingredients.append({'lot': lot.code, 'percent': str(share.percent), 'seal': sample.seal,
                            'panel_id': panel.id, 'scores': {key: getattr(panel, key) for key in ATTRIBUTES}})
    policy = latest_policy(db)
    snapshot = {'code': recipe.code, 'revision': recipe.revision, 'ingredients': ingredients,
                'policy_version': policy.id, 'policy_floor': str(policy.floor)}
    review = GradeReview(recipe_id=recipe.id, revision=recipe.revision, threshold_id=policy.id,
                         state=State.QUEUED, snapshot=snapshot, digest=digest(snapshot), generation=0)
    review.evidence.append(GradeEvidence(generation=0, cause='initial',
                                         snapshot=copy.deepcopy(snapshot), digest=digest(snapshot)))
    return review


def wake(review_id, generation=None):
    message = str(review_id) if generation is None else f'{review_id}:{generation}'
    try:
        redis.rpush(QUEUE, message)
    except RedisError:
        logger.warning('Redis notification failed for review %s; database polling will recover', review_id)


def evidence_for(db, review, generation=None):
    query = select(GradeEvidence).filter_by(review_id=review.id)
    if generation is not None:
        return db.scalar(query.filter_by(generation=generation))
    return db.scalar(query.where(GradeEvidence.superseded_at.is_(None)).order_by(GradeEvidence.generation.desc()).limit(1))


def repanel_panel(db, review):
    return db.scalar(select(SensoryPanel).filter(
        SensoryPanel.review_id == review.id,
        SensoryPanel.invalidated_at.is_(None),
    ).order_by(SensoryPanel.id.desc()).limit(1))


def expect(review, *states):
    if review.state not in states:
        raise HTTPException(409, f'审核状态 {review.state} 不支持该操作')


def independent(db, review, user):
    recipe = require(db, BlendRecipe, review.recipe_id)
    if recipe.author_id == user.id:
        raise HTTPException(403, '配方作者不能复评或审核自己的配方')
    return recipe


def repanel(db, review, user, data):
    independent(db, review, user)
    expect(review, State.REPANEL)
    db.add(SensoryPanel(review_id=review.id, author_id=user.id, **data.model_dump()))
    review.state = State.VERIFY
    record(db, user, 'panel.recheck', review.id, data.note)


def replace_scores(snapshot, panel_id, panel):
    replaced = copy.deepcopy(snapshot)
    touched = False
    for ingredient in replaced['ingredients']:
        if ingredient['panel_id'] == panel_id:
            ingredient['panel_id'] = panel.id
            ingredient['scores'] = {key: getattr(panel, key) for key in ATTRIBUTES}
            touched = True
    return replaced if touched else None


def correct_panel(db, lot, panel, user, data):
    """Supervisor correction of one lot's sensory record.

    The wrong record is retired rather than rewritten, so every signed grade can still be
    traced to the scores it was issued on. Unsigned reviews that froze the old record are
    voided back to the queue with just that one score swapped out.
    """
    if panel.lot_id != lot.id:
        raise HTTPException(404, '该感官单不属于此原料批次')
    if panel.superseded_at is not None or panel.invalidated_at is not None:
        raise HTTPException(409, '这笔评分已被更正或作废，请刷新后核对')
    values = data.model_dump(exclude={'reason'})
    corrected = SensoryPanel(lot_id=lot.id, author_id=user.id, supersedes_id=panel.id, **values)
    db.add(corrected)
    db.flush()
    panel.superseded_at = now()

    affected = []
    # The lot is already locked, so a concurrent submit either committed (visible below) or
    # will read the corrected panel after us. Take only reviews that froze this exact panel.
    candidates = db.execute(select(GradeReview.id, GradeReview.snapshot).where(
        GradeReview.state.notin_((State.APPROVED, State.SEALED))).order_by(GradeReview.id)).all()
    for review_id, frozen in candidates:
        if not any(item['panel_id'] == panel.id for item in frozen['ingredients']):
            continue
        review = require(db, GradeReview, review_id, lock=True)
        # A concurrent sign-off may have landed while we waited for the row lock; the first
        # commit wins, so an already-signed grade keeps its basis untouched.
        if review.state in (State.APPROVED, State.SEALED):
            continue
        snapshot = replace_scores(review.snapshot, panel.id, corrected)
        if snapshot is None:
            continue
        for old in db.scalars(select(SensoryPanel).filter(
                SensoryPanel.review_id == review.id,
                SensoryPanel.invalidated_at.is_(None))):
            old.invalidated_at = now()
        previous = evidence_for(db, review)
        if previous is not None:
            previous.superseded_at = now()
        review.generation += 1
        review.snapshot = snapshot
        review.digest = digest(snapshot)
        review.result = None
        review.failure = None
        review.verified_by = None
        review.signed_by = None
        review.final_grade = None
        review.state = State.QUEUED
        review.evidence.append(GradeEvidence(
            generation=review.generation, cause='score_correction', note=data.reason,
            snapshot=copy.deepcopy(snapshot), digest=review.digest))
        affected.append(review)
    record(db, user, 'panel.correct', f'{lot.code} · 审评单 #{panel.id}',
           f'更正为审评单 #{corrected.id} · {data.reason} · 重算审核 {len(affected)} 张')
    return corrected, affected


def verify(db, review, user, note):
    independent(db, review, user)
    expect(review, State.VERIFY)
    panel = repanel_panel(db, review)
    review.final_grade = panel.grade if panel else review.result['candidate']
    review.verified_by = user.id
    review.state = State.SIGNOFF
    record(db, user, 'grade.verify', review.id, note)


def approve(db, review, user, note):
    independent(db, review, user)
    expect(review, State.SIGNOFF)
    if review.verified_by == user.id:
        raise HTTPException(403, '签发和初审须由两人分别完成')
    review.signed_by = user.id
    review.state = State.APPROVED
    record(db, user, 'grade.signoff', review.id, note)

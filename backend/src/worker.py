import logging
from pathlib import Path

from redis import RedisError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from .constants import ReviewState as State
from .db import QUEUE, redis, transaction
from .grade_model import digest, run
from .models import GradeReview
from .workflow import evidence_for, record

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)
heartbeat = Path('/work/worker.touch')


def consume(review_id=None, generation=None):
    with transaction() as db:
        query = select(GradeReview).where(GradeReview.state == State.QUEUED)
        if review_id is not None:
            query = query.where(GradeReview.id == review_id)
        review = db.scalar(query.order_by(GradeReview.id).limit(1).with_for_update(skip_locked=True))
        if review is None:
            return
        if generation is not None and review.generation != generation:
            # A correction landed before this notification was delivered; its stale input
            # must never reach review, so the newer generation recomputes instead.
            logger.info('review=%s discard stale generation=%s current=%s', review.id, generation, review.generation)
            return
        review.attempts += 1
        try:
            if digest(review.snapshot) != review.digest:
                raise ValueError('冻结快照摘要不匹配')
            review.result = run(review.snapshot)
            review.state = State.REPANEL if review.result['needs_repanel'] else State.VERIFY
            detail = f'{review.digest} · {review.state} · {review.result["confidence"]:.6f}'
            basis = evidence_for(db, review, review.generation)
            if basis is not None:
                basis.result = review.result
        except Exception as exc:
            logger.exception('Grade review %s failed', review.id)
            review.state = State.FAILED
            review.failure = f'{type(exc).__name__}: {exc}'[:350]
            detail = review.failure
        record(db, 'worker', 'grade.compute', review.id, detail)
        logger.info('review=%s generation=%s state=%s attempts=%s',
                    review.id, review.generation, review.state, review.attempts)


def parse(message):
    text = message.decode()
    review_id, _, generation = text.partition(':')
    if not review_id.isdigit():
        return None, None
    return int(review_id), int(generation) if generation.isdigit() else None


def main():
    import time

    while True:
        try:
            try:
                message = redis.blpop(QUEUE, timeout=2)
            except RedisError:
                message = None
                time.sleep(2)
            if message:
                review_id, generation = parse(message[1])
                if review_id is not None:
                    consume(review_id, generation)
            consume()
            heartbeat.touch()
        except SQLAlchemyError:
            logger.exception('Database unavailable, retrying queue')
            time.sleep(2)


if __name__ == '__main__':
    main()

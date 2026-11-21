import logging
import os
import time
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from scripts.seed_demo import seed
from .operations_evidence import router as operations_evidence
from .db import Base, engine, redis, transaction
from .routes import router
from .workflow import record

logger = logging.getLogger('uvicorn.error')

# Columns added after the first release; existing named volumes keep their data.
UPGRADES = (
    ('grade_review', 'generation', 'INTEGER NOT NULL DEFAULT 0'),
    ('sensory_panel', 'supersedes_id', 'INTEGER NULL'),
    ('sensory_panel', 'superseded_at', 'DATETIME NULL'),
    ('sensory_panel', 'invalidated_at', 'DATETIME NULL'),
)


def upgrade():
    """Bring a pre-existing tearoom database up to the current model without losing records."""
    if engine.dialect.name != 'mysql':
        return
    with engine.begin() as conn:
        for table, column, ddl in UPGRADES:
            found = conn.exec_driver_sql(
                'SELECT 1 FROM information_schema.columns WHERE table_schema=DATABASE() '
                'AND table_name=%s AND column_name=%s', (table, column)).first()
            if not found:
                conn.exec_driver_sql(f'ALTER TABLE {table} ADD COLUMN {column} {ddl}')
        # An earlier schema allowed one repanel per review; corrections now retire the old
        # repanel and add another, so the unique index has to give way to a plain one.
        # The replacement index must exist first: MySQL keeps the FK's index requirement.
        unique = [row[0] for row in conn.exec_driver_sql(
            'SELECT DISTINCT index_name FROM information_schema.statistics WHERE table_schema=DATABASE() '
            'AND table_name=%s AND column_name=%s AND non_unique=0 AND index_name<>%s',
            ('sensory_panel', 'review_id', 'PRIMARY')).all()]
        if unique:
            plain = conn.exec_driver_sql(
                'SELECT 1 FROM information_schema.statistics WHERE table_schema=DATABASE() '
                'AND table_name=%s AND index_name=%s',
                ('sensory_panel', 'ix_sensory_panel_review_id')).first()
            if not plain:
                conn.exec_driver_sql('CREATE INDEX ix_sensory_panel_review_id ON sensory_panel (review_id)')
            for name in unique:
                conn.exec_driver_sql(f'ALTER TABLE sensory_panel DROP INDEX `{name}`')
        # Every frozen snapshot becomes its first (and so far only) grading basis.
        conn.exec_driver_sql("""
            INSERT INTO grade_evidence (review_id, generation, cause, snapshot, digest, created_at)
            SELECT r.id, 0, 'initial', r.snapshot, r.digest, r.created_at
            FROM grade_review r LEFT JOIN grade_evidence e ON e.review_id = r.id
            WHERE e.id IS NULL
        """)


@asynccontextmanager
async def lifecycle(app):
    Base.metadata.create_all(engine)
    upgrade()
    if os.getenv('SEED_DEMO', 'true').lower() == 'true':
        try:
            seed()
        except Exception:
            logger.exception('Demo seed failed')
    yield


app = FastAPI(title='岩册 · 茶叶拼配等级审核', lifespan=lifecycle)
app.include_router(operations_evidence)
app.include_router(router)


@app.get('/health')
def health():
    with engine.connect() as conn:
        conn.exec_driver_sql('SELECT 1')
    redis.ping()
    return {'status': 'ok'}


@app.middleware('http')
async def response_headers(request, call_next):
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.middleware('http')
async def request_log(request, call_next):
    start = time.monotonic()
    request_id = uuid4().hex
    response = await call_next(request)
    response.headers['X-Request-ID'] = request_id
    if request.url.path != '/health':
        logger.info('%s %s %s %s %.0fms', request_id, request.method, request.url.path,
                    response.status_code, (time.monotonic() - start) * 1000)
    return response


def reject(request, status, detail, headers=None):
    username = getattr(request.state, 'actor', None)
    if username and request.method in {'POST', 'PUT'}:
        try:
            with transaction() as db:
                record(db, username, 'action.denied', request.url.path[:190], detail)
        except SQLAlchemyError:
            logger.exception('Could not record rejected operation')
    return JSONResponse({'detail': detail}, status_code=status, headers=headers)


@app.exception_handler(HTTPException)
def http_error(request: Request, exc: HTTPException):
    return reject(request, exc.status_code, exc.detail, exc.headers)


@app.exception_handler(IntegrityError)
def conflict(request: Request, exc: IntegrityError):
    return reject(request, 409, '编号、封签或关联记录已存在，请刷新并核对')


@app.exception_handler(RequestValidationError)
def invalid(request: Request, exc: RequestValidationError):
    detail = '；'.join(f'{" / ".join(map(str, error["loc"][1:]))}：{error["msg"]}' for error in exc.errors())
    return reject(request, 422, detail)

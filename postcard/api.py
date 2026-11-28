"""Small HTTP adapter using the standard library. Run one server per SQLite DB."""
import argparse
import hashlib
import io
import json
import logging
import queue
import re
import socket
import sqlite3
import threading
import time
import warnings
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4
from PIL import Image as PILImage, UnidentifiedImageError
from .api_contract import ID_PATTERN, KEY_PATTERN, MAX_FILE, MAX_OPERATIONS_BYTES, MAX_PIXELS, contract
from .contracts import Image, ToolContext
from .durable_dispatcher import DurableDispatcher
from .errors import DomainError, InvalidParams, InvalidTransition
from .job import Job
from .registry import Registry
from .sqlite_repository import SQLiteJobRepository, encode
from .tools import FrameTool, FilterTool

logger = logging.getLogger(__name__)


class APIError(Exception):
    def __init__(self, status_code, code, message, **details):
        self.status, self.code, self.message, self.details = status_code, code, message, details


def job_json(job):
    return {'id': job.id, 'status': job.status,
            'operations': [{'operation': item['name'], 'params': item.get('params', {})} for item in job.operations],
            'cancel_requested': job.cancel_requested, 'error': job.error,
            'warnings': [vars(warning) for warning in job.warnings], **job.times}


class Application:
    def __init__(self, database, start_worker=True):
        self.repository = SQLiteJobRepository(database)
        registry = Registry()
        registry.register(FrameTool())
        registry.register(FilterTool())
        self.dispatcher = DurableDispatcher(registry, self.repository, ToolContext(None, None, datetime.now(timezone.utc)))
        self.dispatcher.recover_after_restart()  # Migrations must already have been run.
        self.pending = queue.Queue()
        self.worker = None
        for job in self.repository.all():
            if job.status == 'created':
                self.pending.put(job.id)
        if start_worker:
            self.worker = threading.Thread(target=self._work, name='postcard-worker')
            self.worker.start()

    def _work(self):
        while True:
            job_id = self.pending.get()
            try:
                if job_id is None:
                    return
                self.dispatcher.run(job_id)
            except InvalidTransition:
                pass  # A queued Job may have been cancelled before start.
            except Exception:
                logger.exception('Worker storage failure for %s', job_id)
            finally:
                self.pending.task_done()

    def close(self):
        if self.worker is not None:
            self.pending.put(None)
            self.worker.join()
            self.worker = None

    def create(self, key, image_bytes, raw_operations):
        try:
            operations = json.loads(raw_operations, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (ValueError, UnicodeError):
            raise APIError(422, 'invalid_params', 'operations must be valid JSON', field='operations') from None
        canonical = json.dumps(operations, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
        # Length-prefix the image to avoid ambiguity between the two concatenated fields.
        fingerprint = hashlib.sha256(len(image_bytes).to_bytes(8, 'big') + image_bytes + canonical).hexdigest()
        replay = self.repository.replay(key, fingerprint)
        if replay is not None:
            return replay, False
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('error', PILImage.DecompressionBombWarning)
                with PILImage.open(io.BytesIO(image_bytes)) as pixels:
                    if pixels.format not in ('JPEG', 'PNG'):
                        raise APIError(422, 'invalid_image', 'Only JPEG and PNG are accepted', reason='unsupported_format')
                    if pixels.width * pixels.height > MAX_PIXELS:
                        raise APIError(413, 'payload_too_large', 'Image exceeds pixel limit', limit='megapixels', max=MAX_PIXELS)
                    pixels.verify()
                with PILImage.open(io.BytesIO(image_bytes)) as pixels:
                    image = Image(pixels)
        except (PILImage.DecompressionBombError, PILImage.DecompressionBombWarning):
            raise APIError(413, 'payload_too_large', 'Image exceeds pixel limit', limit='megapixels', max=MAX_PIXELS) from None
        except (UnidentifiedImageError, OSError, ValueError, SyntaxError):
            raise APIError(422, 'invalid_image', 'Invalid or damaged image', reason='corrupted') from None
        if not isinstance(operations, list) or not 1 <= len(operations) <= 10:
            raise InvalidParams('operations', 'Expected 1 to 10 operations')
        plan = []
        for operation in operations:
            if not isinstance(operation, dict) or set(operation) - {'operation', 'params'}:
                raise InvalidParams('operations', 'Unknown operation fields')
            plan.append({'name': operation.get('operation'), 'params': operation.get('params', {})})
        self.dispatcher.validate_plan(plan)
        candidate = Job('j_' + uuid4().hex, image, plan)
        job, created = self.repository.create_once(candidate, key, fingerprint)
        if created:
            self.pending.put(job.id)
        return job, created


class Handler(BaseHTTPRequestHandler):
    server_version = 'Postcard/0.4'

    def log_message(self, *_):
        pass  # Do not write secret Job ids to the default access log.

    def _send(self, status, body, content_type='application/json', headers=None):
        data = json.dumps(body, ensure_ascii=False).encode() if content_type == 'application/json' else body
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('X-Correlation-ID', self.correlation)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Connection', 'close')
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.close_connection = True
        if self.command != 'HEAD':
            self.wfile.write(data)

    def send_error(self, code, message=None, explain=None):
        # Includes unknown HTTP methods and parser errors handled by stdlib.
        self.correlation = uuid4().hex
        self._send(code, {'code': 'bad_request', 'message': 'Invalid HTTP request', 'details': {}})

    def _multipart(self):
        key = self.headers.get('Idempotency-Key', '')
        if not re.fullmatch(KEY_PATTERN, key):
            raise APIError(400, 'missing_idempotency_key', 'A valid Idempotency-Key is required')
        if self.headers.get('Transfer-Encoding'):
            raise APIError(400, 'bad_request', 'Chunked upload is not supported')
        lengths = self.headers.get_all('Content-Length', [])
        if not lengths:
            raise APIError(411, 'bad_request', 'Content-Length is required')
        if len(lengths) != 1 or not lengths[0].isdigit():
            raise APIError(400, 'bad_request', 'Invalid Content-Length')
        length = int(lengths[0])
        if length > MAX_FILE + MAX_OPERATIONS_BYTES + 65536:
            raise APIError(413, 'payload_too_large', 'Request too large', limit='file_size', max=MAX_FILE)
        body = bytearray()
        deadline = time.monotonic() + 30
        while len(body) < length:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise APIError(408, 'request_timeout', 'Receive deadline exceeded')
            self.connection.settimeout(remaining)
            chunk = self.rfile.read1(min(65536, length - len(body)))
            if not chunk:
                raise APIError(400, 'bad_request', 'Truncated request')
            body.extend(chunk)
        content_type = self.headers.get('Content-Type', '')
        message = BytesParser(policy=policy.default).parsebytes(
            ('Content-Type: ' + content_type + '\r\nMIME-Version: 1.0\r\n\r\n').encode() + body)
        if message.get_content_type() != 'multipart/form-data' or not message.is_multipart() or message.defects:
            raise APIError(400, 'bad_request', 'Expected multipart/form-data')
        parts = {}
        for part in message.iter_parts():
            name = part.get_param('name', header='content-disposition')
            if name not in ('image', 'operations') or name in parts or part.is_multipart() or part.defects:
                raise APIError(400, 'bad_request', 'Expected one image and one operations part')
            parts[name] = part.get_payload(decode=True)
        if set(parts) != {'image', 'operations'}:
            raise APIError(400, 'bad_request', 'image and operations are required')
        for name, maximum, limit in [('image', MAX_FILE, 'file_size'), ('operations', MAX_OPERATIONS_BYTES, 'operations_size')]:
            if len(parts[name]) > maximum:
                raise APIError(413, 'payload_too_large', 'Part too large', limit=limit, max=maximum)
        return key, parts['image'], parts['operations']

    def _route(self):
        path = self.path.split('?', 1)[0]
        app = self.server.app
        if path == '/openapi.json' and self.command == 'GET':
            return self._send(200, contract())
        if path == '/health' and self.command == 'GET':
            connection = app.repository.connect()
            try:
                connection.execute('SELECT id FROM jobs LIMIT 1').fetchone()
            finally:
                connection.close()
            return self._send(200, {'status': 'ok', 'checks': {'database': 'ok', 'storage': 'ok'}})
        if path == '/jobs' and self.command == 'POST':
            job, created = app.create(*self._multipart())
            return self._send(202 if created else 200, job_json(job), headers=(
                {'Location': '/jobs/' + job.id} if created else {'Idempotent-Replayed': 'true'}))
        match = re.fullmatch(r'/jobs/([^/]+)(/result|/cancel)?', path)
        if match:
            job_id, suffix = match.groups()
            expected = 'POST' if suffix == '/cancel' else 'GET'
            if self.command != expected:
                raise APIError(405, 'bad_request', 'Method not allowed')
            if not re.fullmatch(ID_PATTERN, job_id):
                raise KeyError(job_id)
            job = app.repository.get(job_id)
            if suffix == '/cancel':
                job = app.repository.mutate(job_id, lambda current: current.cancel())
            elif suffix == '/result':
                if job.status in ('created', 'running'):
                    raise APIError(409, 'result_not_ready', 'Result is not ready', status=job.status)
                if job.status != 'done':
                    raise APIError(409, 'result_not_available', 'Job has no result', status=job.status)
                if datetime.fromisoformat(job.times['result_expires_at']) <= app.repository.clock():
                    raise APIError(410, 'result_expired', 'Result has expired', result_expires_at=job.times['result_expires_at'])
                return self._send(200, encode(job.result), 'image/png')
            return self._send(200, job_json(job))
        if path in ('/jobs', '/health', '/openapi.json'):
            raise APIError(405, 'bad_request', 'Method not allowed')
        raise APIError(404, 'job_not_found', 'Resource not found')

    def _handle(self):
        supplied = self.headers.get('X-Correlation-ID', '')
        self.correlation = supplied if re.fullmatch(r'[A-Za-z0-9_-]{1,128}', supplied) else uuid4().hex
        try:
            return self._route()
        except APIError as error:
            status, code, message, details = error.status, error.code, error.message, error.details
        except KeyError:
            status, code, message, details = 404, 'job_not_found', 'Resource not found', {}
        except DomainError as error:
            status = 409 if error.code in ('invalid_transition', 'idempotency_key_conflict') else 422
            code, message, details = error.code, error.message, error.details
        except (TimeoutError, socket.timeout):
            status, code, message, details = 408, 'request_timeout', 'Receive deadline exceeded', {}
        except sqlite3.Error:
            logger.exception('Database failure')
            status, code, message, details = 503, 'service_unavailable', 'Storage is unavailable', {}
        except Exception:
            logger.exception('Request failed')
            status, code, message, details = 500, 'internal_error', 'Internal service error', {}
        self._send(status, {'code': code, 'message': message, 'details': details})

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _handle


class Server(ThreadingHTTPServer):
    daemon_threads = False

    def __init__(self, address, app):
        self.app = app
        super().__init__(address, Handler)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--database', default='postcard.sqlite3')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8000)
    args = parser.parse_args()
    app = Application(args.database)
    server = Server((args.host, args.port), app)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        app.close()


if __name__ == '__main__':
    main()

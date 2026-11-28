import io
import json
import tempfile
import threading
import time
import unittest
from http.client import HTTPConnection
from pathlib import Path
from PIL import Image as PILImage
from postcard.api import Application, Server
from postcard.migrations import migrate


def png(color='red'):
    stream = io.BytesIO()
    PILImage.new('RGB', (30, 20), color).save(stream, format='PNG')
    return stream.getvalue()


def multipart(image=None, operations=None):
    boundary = 'p4-boundary-2b25b49b'
    if image is None:
        image = png()
    if operations is None:
        operations = [{'operation': 'filter', 'params': {'name': 'grayscale'}}]
    raw = operations if isinstance(operations, bytes) else json.dumps(operations).encode()
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="photo.png"\r\n'
            'Content-Type: image/png\r\n\r\n').encode() + image
    body += (f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="operations"\r\n'
             'Content-Type: application/json\r\n\r\n').encode() + raw
    body += f'\r\n--{boundary}--\r\n'.encode()
    return body, {'Content-Type': f'multipart/form-data; boundary={boundary}'}


class APIHarness(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / 'jobs.sqlite3'
        migrate(self.database)
        self.start()

    def start(self, worker=True):
        self.app = Application(self.database, start_worker=worker)
        self.server = Server(('127.0.0.1', 0), self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.01})
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        self.app.close()

    def tearDown(self):
        self.stop()
        self.temp.cleanup()

    def request(self, method, path, body=None, headers=None):
        connection = HTTPConnection(*self.server.server_address, timeout=5)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            data = response.read()
            if response.getheader('Content-Type') == 'application/json':
                data = json.loads(data)
            return response.status, dict(response.getheaders()), data
        finally:
            connection.close()

    def create(self, key='example_key', image=None, operations=None):
        body, headers = multipart(image, operations)
        if key is not None:
            headers['Idempotency-Key'] = key
        return self.request('POST', '/jobs', body, headers)

    def wait_done(self, job_id):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status, _, job = self.request('GET', '/jobs/' + job_id)
            self.assertEqual(status, 200)
            if job['status'] in ('done', 'failed', 'cancelled'):
                self.assertEqual(job['status'], 'done', job)
                return job
            time.sleep(0.01)
        self.fail('Worker did not finish')


class APITests(APIHarness):
    def test_normal_path_and_replay(self):
        status, headers, job = self.create()
        self.assertEqual(status, 202)
        self.assertEqual(headers['Location'], '/jobs/' + job['id'])
        self.wait_done(job['id'])
        status, headers, data = self.request('GET', '/jobs/' + job['id'] + '/result')
        self.assertEqual(status, 200)
        self.assertEqual(headers['Content-Type'], 'image/png')
        with PILImage.open(io.BytesIO(data)) as image:
            self.assertEqual(image.size, (30, 20))
        status, headers, repeated = self.create()
        self.assertEqual(status, 200)
        self.assertEqual(headers['Idempotent-Replayed'], 'true')
        self.assertEqual(job['id'], repeated['id'])
        self.assertEqual(len(self.app.repository.all()), 1)
        self.assertEqual(self.create(image=png('blue'))[0], 409)

    def test_errors_share_format_and_correct_status(self):
        cases = [self.create(key=None), self.create(image=b'broken'),
                 self.create(operations=[{'operation': 'sparkle'}]),
                 self.request('GET', '/jobs/missing'), self.request('GET', '/jobs')]
        self.assertEqual([item[0] for item in cases], [400, 422, 422, 404, 405])
        for _, _, error in cases:
            self.assertEqual(set(error), {'code', 'message', 'details'})
            self.assertIsInstance(error['details'], dict)
        self.assertEqual(self.app.repository.all(), ())

    def test_framework_and_internal_errors_are_sanitized(self):
        original = self.app.repository.get
        self.app.repository.get = lambda _: (_ for _ in ()).throw(RuntimeError('SQL private/file/path'))
        try:
            with self.assertLogs('postcard.api', level='ERROR'):
                status, _, error = self.request('GET', '/jobs/j_' + 'a' * 32)
            self.assertEqual(status, 500)
            self.assertNotIn('private', json.dumps(error))
            self.assertEqual(error['details'], {})
        finally:
            self.app.repository.get = original

    def test_cancel_and_early_result(self):
        self.stop()
        self.start(worker=False)
        _, _, job = self.create()
        self.assertEqual(self.request('GET', '/jobs/' + job['id'] + '/result')[0], 409)
        self.assertEqual(self.request('POST', '/jobs/' + job['id'] + '/cancel')[2]['status'], 'cancelled')
        status, _, error = self.request('POST', '/jobs/' + job['id'] + '/cancel')
        self.assertEqual((status, error['code']), (409, 'invalid_transition'))

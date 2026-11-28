"""Single source of truth for the p4 OpenAPI artifact and response schemas."""
import json
from pathlib import Path

MAX_FILE = 10 * 1024 * 1024
MAX_OPERATIONS_BYTES = 16 * 1024
MAX_PIXELS = 25_000_000
ID_PATTERN = r'^j_[0-9a-f]{32}$'
KEY_PATTERN = r'^[A-Za-z0-9_-]{8,64}$'


def obj(properties, required=None):
    return {'type': 'object', 'properties': properties,
            'required': list(properties) if required is None else required, 'additionalProperties': False}


ERROR_SCHEMA = obj({'code': {'type': 'string'}, 'message': {'type': 'string'}, 'details': {'type': 'object'}})
OPERATIONS_SCHEMA = {'type': 'array', 'minItems': 1, 'maxItems': 10, 'items': {'oneOf': [
    obj({'operation': {'const': 'frame'}, 'params': obj({
        'style': {'enum': ['thin', 'classic']}, 'width': {'type': 'integer', 'minimum': 1, 'maximum': 15},
        'color': {'type': 'string', 'pattern': '^#[0-9A-Fa-f]{6}$'}}, [])}, ['operation']),
    obj({'operation': {'const': 'filter'}, 'params': obj({'name': {'enum': ['sepia', 'grayscale']}})})
]}}
TIME = {'type': ['string', 'null'], 'format': 'date-time'}
JOB_SCHEMA = obj({'id': {'type': 'string', 'pattern': ID_PATTERN},
                  'status': {'enum': ['created', 'running', 'done', 'failed', 'cancelled']},
                  'operations': OPERATIONS_SCHEMA, 'cancel_requested': {'type': 'boolean'},
                  'created_at': {'type': 'string', 'format': 'date-time'}, 'started_at': TIME,
                  'finished_at': TIME, 'updated_at': {'type': 'string', 'format': 'date-time'},
                  'result_expires_at': TIME, 'error': {'anyOf': [ERROR_SCHEMA, {'type': 'null'}]},
                  'warnings': {'type': 'array', 'items': obj({'code': {'type': 'string'}, 'message': {'type': 'string'}})}})


def contract():
    def response(schema):
        return {'description': 'Response', 'content': {'application/json': {'schema': schema}}}
    errors = {str(status): response(ERROR_SCHEMA) for status in (400, 404, 405, 408, 409, 410, 411, 413, 422, 500, 503)}
    def route(method, success):
        return {method: {'responses': {**errors, **success}}}
    job = response({'$ref': '#/components/schemas/Job'})
    create = route('post', {'200': job, '202': job})
    create['post'].update({
        'summary': 'Create or replay a background Job',
        'description': 'First request: 202 + Location. Replay: 200 + Idempotent-Replayed: true, current Job. '
                       'Key TTL: 24 hours. Conflict: 409. JPEG/PNG <=10 MiB, <=25 megapixels; '
                       'operations JSON <=16 KiB, 1..10 items. Content-Length required; chunked upload unsupported. '
                       'Receive deadline: 30s. No authentication; knowledge of a random Job id grants access.',
        'parameters': [{'name': 'Idempotency-Key', 'in': 'header', 'required': True,
                        'schema': {'type': 'string', 'pattern': KEY_PATTERN}}],
        'requestBody': {'required': True, 'content': {'multipart/form-data': {'schema': obj({
            'image': {'type': 'string', 'format': 'binary'}, 'operations': OPERATIONS_SCHEMA}),
            'encoding': {'operations': {'contentType': 'application/json'}}}}}})
    paths = {'/jobs': create, '/jobs/{jobId}': route('get', {'200': job}),
             '/jobs/{jobId}/result': route('get', {'200': {'description': 'PNG result',
                  'content': {'image/png': {'schema': {'type': 'string', 'format': 'binary'}}}}}),
             '/jobs/{jobId}/cancel': route('post', {'200': job}),
             '/health': route('get', {'200': response(obj({'status': {'const': 'ok'},
                   'checks': obj({'database': {'const': 'ok'}, 'storage': {'const': 'ok'}})}))})}
    for path in paths:
        if '{jobId}' in path:
            paths[path]['parameters'] = [{'name': 'jobId', 'in': 'path', 'required': True,
                                          'schema': {'type': 'string', 'pattern': ID_PATTERN}}]
    return {'openapi': '3.1.0', 'info': {'title': 'Postcard Service API', 'version': '0.4.0',
            'description': 'p4 implements frame/filter. Other planned tools remain deferred. '
                           'PNG inputs/results are persisted as SQLite BLOBs for atomic publication.'},
            'paths': paths, 'components': {'schemas': {'Job': JOB_SCHEMA, 'Error': ERROR_SCHEMA}}}


def write_contract():
    text = json.dumps(contract(), ensure_ascii=False, indent=2) + '\n'
    directory = Path(__file__).resolve().parent.parent / 'docs' / 'api'
    # JSON is also valid YAML: keep the established .yaml link working.
    (directory / 'openapi.yaml').write_text(text, encoding='utf-8')
    (directory / 'openapi.json').write_text(text, encoding='utf-8')


if __name__ == '__main__':
    write_contract()

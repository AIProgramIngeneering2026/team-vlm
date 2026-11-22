import logging
from copy import deepcopy
from uuid import uuid4
from .errors import InvalidParams, InvalidTransition, ToolExecutionError, UnknownOperation
from .job import Job
from .repository import JobRepository

logger = logging.getLogger(__name__)


class Dispatcher:
    def __init__(self, registry, repository: JobRepository, context):
        self.registry, self.repository, self.context = registry, repository, context

    def validate_plan(self, operations):
        if not isinstance(operations, list) or not operations:
            raise InvalidParams('operations', 'Expected a nonempty list')
        for index, operation in enumerate(operations):
            try:
                if not isinstance(operation, dict):
                    raise InvalidParams('operations', 'Expected an operation object')
                for key in operation:
                    if key not in ('name', 'params'):
                        raise InvalidParams(key, 'Unknown operation field')
                name = operation.get('name')
                if not isinstance(name, str):
                    raise InvalidParams('name', 'Expected operation name')
                tool = self.registry.get(name)
                params = operation.get('params', {})
                if not isinstance(params, dict):
                    raise InvalidParams('params', 'Expected an object')
                tool.validate(params)
            except (InvalidParams, UnknownOperation) as error:
                error.details.update(operation_index=index, operation=operation.get('name') if isinstance(operation, dict) else None)
                raise

    def create(self, image, operations):
        plan = deepcopy(operations)
        self.validate_plan(plan)
        job = Job(str(uuid4()), image, plan)
        self.repository.add(job)
        return job

    def _cancelled(self, job):
        if job.cancel_requested:
            job.acknowledge_cancel()
            return True
        return False

    def run(self, job_id):
        job = self.repository.get(job_id)
        job.start()  # Outside the handler: duplicate worker must not fail the real job.
        image, warnings = job.image, []
        index, name = None, None
        try:
            for index, operation in enumerate(job.operations):
                if self._cancelled(job):
                    return
                name = operation['name']
                result = self.registry.get(name).run(image, operation.get('params', {}), self.context)
                image = result.image
                warnings.extend(result.warnings)
            if not self._cancelled(job):
                try:
                    job.finish(image, warnings)
                except InvalidTransition:
                    # Cancel may win immediately after the previous check.
                    if not self._cancelled(job):
                        raise
        except ToolExecutionError as error:
            job.fail({'code': error.code, 'message': error.message,
                      'details': {**error.details, 'operation_index': index, 'operation': name}})
        except Exception:
            logger.exception('Tool execution failed for job %s', job.id)
            job.fail({'code': 'internal_error', 'message': 'Tool execution failed',
                      'details': {'operation_index': index, 'operation': name}})

    def recover_after_restart(self):
        """Startup hook, called before workers start; never during live work."""
        for job in self.repository.all():
            if job.status == 'running':
                job.restart()

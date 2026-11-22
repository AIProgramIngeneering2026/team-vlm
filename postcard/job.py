from copy import deepcopy
from threading import RLock
from .errors import InvalidTransition


class Job:
    """All state changes and the cancellation/finish race are guarded here.

    Terminal states have no outgoing transitions. No retry or rerun exists.
    Payload snapshots prevent callers from changing a validated plan.
    """
    def __init__(self, job_id, image, operations):
        self.id = job_id
        self.image = image
        self._operations = deepcopy(operations)
        self._status = 'created'
        self._cancel_requested = False
        self._result = None
        self._error = None
        self._warnings = ()
        self._lock = RLock()

    @property
    def operations(self):
        return deepcopy(self._operations)

    @property
    def status(self):
        with self._lock:
            return self._status

    @property
    def cancel_requested(self):
        with self._lock:
            return self._cancel_requested

    @property
    def result(self):
        with self._lock:
            return self._result

    @property
    def error(self):
        with self._lock:
            return deepcopy(self._error)

    @property
    def warnings(self):
        with self._lock:
            return self._warnings

    def _require(self, state, command):
        if self._status != state:
            raise InvalidTransition(self._status, command)

    def start(self):
        with self._lock:
            self._require('created', 'start')
            self._status = 'running'

    def cancel(self):
        with self._lock:
            if self._status == 'created':
                self._status = 'cancelled'
            elif self._status == 'running' and not self._cancel_requested:
                self._cancel_requested = True
            else:
                raise InvalidTransition(self._status, 'cancel')

    def acknowledge_cancel(self):
        with self._lock:
            self._require('running', 'acknowledge_cancel')
            if not self._cancel_requested:
                raise InvalidTransition(self._status, 'acknowledge_cancel')
            self._status = 'cancelled'

    def finish(self, image, warnings=()):
        with self._lock:
            self._require('running', 'finish')
            if self._cancel_requested:
                raise InvalidTransition(self._status, 'finish_after_cancel')
            self._result, self._warnings = image, tuple(warnings)
            self._status = 'done'

    def fail(self, error):
        with self._lock:
            self._require('running', 'fail')
            self._error = deepcopy(error)
            self._status = 'failed'

    def restart(self):
        with self._lock:
            self._require('running', 'restart')
            self._error = {'code': 'interrupted', 'message': 'Worker lost on server restart', 'details': {}}
            self._status = 'failed'

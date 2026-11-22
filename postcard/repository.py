from threading import RLock
from typing import Protocol
from .job import Job


class JobRepository(Protocol):
    def add(self, job: Job) -> None: ...
    def get(self, job_id: str) -> Job: ...
    def all(self) -> tuple[Job, ...]: ...


class MemoryJobRepository:
    def __init__(self):
        self._jobs = {}
        self._lock = RLock()

    def add(self, job):
        with self._lock:
            if job.id in self._jobs:
                raise ValueError('Duplicate job id')
            self._jobs[job.id] = job

    def get(self, job_id):
        with self._lock:
            return self._jobs[job_id]

    def all(self):
        with self._lock:
            return tuple(self._jobs.values())

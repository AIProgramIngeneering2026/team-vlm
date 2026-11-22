import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Barrier, Event, Thread
from PIL import Image as PILImage
from postcard.contracts import Image, ToolContext, ToolResult, Warning
from postcard.dispatcher import Dispatcher
from postcard.errors import DomainError, InvalidParams, InvalidTransition, ToolExecutionError, UnknownOperation
from postcard.job import Job
from postcard.registry import Registry
from postcard.repository import MemoryJobRepository
from postcard.tools import FrameTool, FilterTool


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.image = Image(PILImage.new('RGB', (100, 80), '#804020'))
        self.registry = Registry()
        self.registry.register(FrameTool())
        self.registry.register(FilterTool())
        self.repo = MemoryJobRepository()
        self.dispatcher = Dispatcher(self.registry, self.repo, ToolContext(None, None, datetime(2026, 11, 21, tzinfo=timezone.utc)))
        self.job = self.dispatcher.create(self.image, [{'name': 'frame', 'params': {}}])

    def test_normal_path(self):
        self.assertEqual(self.job.status, 'created')
        self.dispatcher.run(self.job.id)
        self.assertEqual(self.job.status, 'done')
        self.assertEqual(self.job.result.pixels().getpixel((0, 0)), (255, 255, 255))

    def test_z1_double_start(self):
        self.job.start()
        with self.assertRaises(InvalidTransition): self.job.start()
        self.assertEqual(self.job.status, 'running')

    def test_z2_restart(self):
        self.job.start()
        self.dispatcher.recover_after_restart()
        self.assertEqual(self.job.status, 'failed')
        self.assertEqual(self.job.error['code'], 'interrupted')
        with self.assertRaises(InvalidTransition): self.job.finish(self.image)

    def test_z3_cancel_then_finish(self):
        self.job.start()
        self.job.cancel()
        with self.assertRaises(InvalidTransition): self.job.finish(self.image)
        self.job.acknowledge_cancel()
        with self.assertRaises(InvalidTransition): self.job.finish(self.image)
        self.assertEqual(self.job.status, 'cancelled')
        self.assertIsNone(self.job.result)

    def test_z4_two_outcomes(self):
        self.job.start()
        self.job.fail({'code': 'processing_failed'})
        with self.assertRaises(InvalidTransition): self.job.finish(self.image)
        self.assertEqual(self.job.status, 'failed')

    def test_z5_success_without_work(self):
        with self.assertRaises(InvalidTransition): self.job.finish(self.image)
        self.assertEqual(self.job.status, 'created')

    def test_z6_repeat_success(self):
        self.dispatcher.run(self.job.id)
        with self.assertRaises(InvalidTransition): self.dispatcher.run(self.job.id)
        self.assertEqual(self.job.status, 'done')

    def test_unknown_operation_not_saved(self):
        with self.assertRaises(UnknownOperation) as error:
            self.dispatcher.create(self.image, [{'name': 'missing'}])
        self.assertEqual(error.exception.code, 'unknown_operation')
        self.assertEqual(error.exception.details['operation_index'], 0)
        self.assertEqual(len(self.repo.all()), 1)

    def test_invalid_parameters(self):
        cases = [('frame', {'width': True}, 'width'), ('frame', {'width': 16}, 'width'),
                 ('frame', {'style': 'bad'}, 'style'), ('frame', {'color': '#ZZZZZZ'}, 'color'),
                 ('frame', {'extra': 1}, 'extra'), ('filter', {}, 'name'),
                 ('filter', {'name': 'blue'}, 'name'), ('filter', {'name': 'sepia', 'extra': 1}, 'extra')]
        for name, params, field in cases:
            with self.subTest(name=name, params=params):
                with self.assertRaises(InvalidParams) as error:
                    self.dispatcher.create(self.image, [{'name': name, 'params': params}])
                self.assertEqual(error.exception.code, 'invalid_params')
                self.assertEqual(error.exception.details['field'], field)
                self.assertEqual(error.exception.details['operation_index'], 0)
        self.assertEqual(len(self.repo.all()), 1)

    def test_duplicate_registration(self):
        with self.assertRaises(DomainError): self.registry.register(FrameTool())

    def test_expected_and_unexpected_tool_failure(self):
        for exception, code in [(ToolExecutionError('processing_failed', 'Cannot process'), 'processing_failed'), (RuntimeError('private data'), 'internal_error')]:
            class Broken:
                name = 'broken'
                def validate(self, params): pass
                def run(self, image, params, ctx): raise exception
            registry = Registry()
            registry.register(Broken())
            dispatcher = Dispatcher(registry, self.repo, self.dispatcher.context)
            job = dispatcher.create(self.image, [{'name': 'broken'}])
            with self.assertLogs('postcard.dispatcher') if code == 'internal_error' else self.subTest(code=code):
                dispatcher.run(job.id)
            self.assertEqual(job.status, 'failed')
            self.assertEqual(job.error['code'], code)
            self.assertEqual(job.error['details']['operation'], 'broken')

    def test_extension_warnings_and_operation_order(self):
        class Extra:
            name = 'extra'
            def validate(self, params): pass
            def run(inner, image, params, ctx):
                self.assertEqual(image.pixels().getpixel((0, 0)), (255, 255, 255))
                return ToolResult.ok(image, [Warning('example', 'warning')])
        self.registry.register(Extra())
        job = self.dispatcher.create(self.image, [{'name': 'frame'}, {'name': 'extra'}])
        self.dispatcher.run(job.id)
        self.assertEqual(job.warnings[0].code, 'example')

    def test_tools_pixels_size_metadata_and_source(self):
        source = PILImage.new('RGB', (100, 80), '#804020')
        source.info['exif'] = b'GPS private'
        image = Image(source)
        for tool, params in [(FrameTool(), {}), (FrameTool(), {'style': 'thin'}),
                             (FilterTool(), {'name': 'grayscale'}), (FilterTool(), {'name': 'sepia'})]:
            tool.validate(params)
            result = tool.run(image, params, self.dispatcher.context).image.pixels()
            self.assertEqual(result.size, source.size)
            self.assertFalse(result.info)
            self.assertNotEqual(result.tobytes(), source.tobytes())
        gray = FilterTool().run(image, {'name': 'grayscale'}, self.dispatcher.context).image.pixels()
        r, g, b = gray.getpixel((50, 40))
        self.assertEqual((r, g), (g, b))
        self.assertEqual(image.pixels().tobytes(), source.tobytes())

    def test_cancel_before_start(self):
        self.job.cancel()
        with self.assertRaises(InvalidTransition): self.dispatcher.run(self.job.id)
        self.assertEqual(self.job.status, 'cancelled')

    def test_cancel_during_last_operation(self):
        entered, release = Event(), Event()
        class Blocking:
            name = 'blocking'
            def validate(self, params): pass
            def run(self, image, params, ctx):
                entered.set()
                if not release.wait(5): raise RuntimeError('Test timed out')
                return ToolResult.ok(image)
        self.registry.register(Blocking())
        job = self.dispatcher.create(self.image, [{'name': 'blocking'}, {'name': 'frame'}])
        worker = Thread(target=self.dispatcher.run, args=(job.id,))
        worker.start()
        try:
            self.assertTrue(entered.wait(5))
            job.cancel()
            self.assertEqual(job.status, 'running')
            with self.assertRaises(InvalidTransition): self.dispatcher.run(job.id)
        finally:
            release.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(job.status, 'cancelled')
        self.assertIsNone(job.result)

    def test_cancel_finish_race(self):
        for _ in range(30):
            job = Job('race', self.image, [])
            job.start()
            barrier = Barrier(2)
            def invoke(action):
                barrier.wait()
                try: action(); return True
                except InvalidTransition: return False
            with ThreadPoolExecutor(2) as pool:
                cancel = pool.submit(invoke, job.cancel)
                finish = pool.submit(invoke, lambda: job.finish(self.image))
                cancelled, finished = cancel.result(), finish.result()
            self.assertNotEqual(cancelled, finished)
            if cancelled:
                job.acknowledge_cancel()
                self.assertIsNone(job.result)
            else:
                self.assertEqual(job.status, 'done')

    def test_validated_plan_cannot_be_changed(self):
        plan = [{'name': 'frame', 'params': {'width': 5}}]
        job = self.dispatcher.create(self.image, plan)
        plan[0]['params']['width'] = -1
        exposed = job.operations
        exposed[0]['params']['width'] = -2
        self.assertEqual(job.operations[0]['params']['width'], 5)

    def test_all_terminal_states_reject_commands(self):
        for state in ('done', 'failed', 'cancelled'):
            job = Job(state, self.image, [])
            job.start()
            if state == 'done': job.finish(self.image)
            elif state == 'failed': job.fail({'code': 'example'})
            else: job.cancel(); job.acknowledge_cancel()
            for action in (job.start, job.cancel, job.restart, job.acknowledge_cancel,
                           lambda: job.finish(self.image), lambda: job.fail({})):
                with self.assertRaises(InvalidTransition): action()
                self.assertEqual(job.status, state)


if __name__ == '__main__':
    unittest.main()

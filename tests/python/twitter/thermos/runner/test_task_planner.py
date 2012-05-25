import pytest
from twitter.common.testing.clock import ThreadedClock
from twitter.thermos.config.schema import *
from twitter.thermos.runner.planner import TaskPlanner


p1 = Process(name = "p1", cmdline = "")
p2 = Process(name = "p2", cmdline = "")
p3 = Process(name = "p3", cmdline = "")

unordered_task = Task(name = "unordered", processes = [p1, p2, p3])
ordered_task = unordered_task(constraints = [{'order': ['p1', 'p2', 'p3']}])
empty_task = Task(name = "empty", processes = [])

def _(*processes):
  return set(processes)
empty = set()

def approx_equal(a, b):
  return abs(a - b) < 0.001


def test_task_construction():
  p = TaskPlanner(empty_task)
  assert p.runnable == empty
  assert p.is_complete()
  p = TaskPlanner(unordered_task)
  assert p.runnable == _('p1', 'p2', 'p3')
  assert not p.is_complete()
  p = TaskPlanner(ordered_task)
  assert p.runnable == _('p1')
  assert not p.is_complete()


def test_task_finish_with_ephemerals():
  pure_ephemeral = empty_task(processes=[p1(ephemeral=True)])
  p = TaskPlanner(pure_ephemeral)
  assert p.is_complete()
  p.set_running('p1')
  assert p.is_complete()
  p.add_failure('p1')
  assert p.is_complete()

  with_ephemeral = empty_task(processes=[p1, p2(ephemeral=True)])
  p = TaskPlanner(with_ephemeral)
  assert not p.is_complete()
  assert p.runnable == _('p1', 'p2')
  p.set_running('p1')
  assert not p.is_complete()
  p.add_failure('p1')
  assert p.is_complete()
  p.set_running('p2')
  assert p.is_complete()


def test_task_finish_with_daemons():
  # Daemon is still restricted to the failure limit
  p = TaskPlanner(empty_task(processes=[p1(daemon=True)]))
  assert not p.is_complete()
  p.set_running('p1')
  assert not p.is_complete()
  p.add_failure('p1')
  assert p.is_complete()

  # Resilient to two failures
  p = TaskPlanner(empty_task(processes=[p1(daemon=True, max_failures=2)]))
  assert not p.is_complete()
  p.set_running('p1')
  assert not p.is_complete()
  p.add_failure('p1')
  assert not p.is_complete()
  p.set_running('p1')
  assert not p.is_complete()
  p.add_failure('p1')
  assert p.is_complete()

  # Can swallow successes
  p = TaskPlanner(empty_task(processes=[p1(daemon=True, max_failures=2)]))
  assert not p.is_complete()
  p.set_running('p1')
  assert not p.is_complete()
  p.add_failure('p1')
  assert not p.is_complete()
  p.set_running('p1')
  assert not p.is_complete()
  p.add_success('p1')
  assert not p.is_complete()
  p.set_running('p1')
  assert not p.is_complete()
  p.add_failure('p1')
  assert p.is_complete()


def test_task_finish_with_daemon_ephemerals():
  p = TaskPlanner(empty_task(processes=[p1, p2(daemon=True, ephemeral=True, max_failures=2)]))
  assert not p.is_complete()
  p.set_running('p1')
  p.set_running('p2')
  assert not p.is_complete()
  p.add_success('p1')
  assert p.is_complete()


def test_task_process_cannot_depend_upon_daemon():
  with pytest.raises(TaskPlanner.InvalidSchedule):
    TaskPlanner(empty_task(processes=[p1(daemon=True), p2], constraints=[{'order': ['p1', 'p2']}]))


def test_task_failed_predecessor_does_not_make_process_runnable():
  p = TaskPlanner(empty_task(processes=[p1, p2], constraints=[{'order': ['p1', 'p2']}]))
  p.set_running('p1')
  p.add_success('p1')
  assert 'p2' in p.runnable
  assert not p.is_complete()

  p = TaskPlanner(empty_task(processes=[p1, p2], constraints=[{'order': ['p1', 'p2']}]))
  p.set_running('p1')
  p.add_failure('p1')
  assert 'p2' not in p.runnable
  assert not p.is_complete()


def test_task_daemon_duration():
  p = TaskPlanner(empty_task(processes=[p1(daemon=True, max_failures=2, min_duration=10)]))
  assert 'p1' in p.runnable
  p.set_running('p1')
  p.add_success('p1', timestamp=5)
  assert 'p1' not in p.runnable_at(timestamp=5)
  assert 'p1' not in p.runnable_at(timestamp=10)
  assert 'p1' in p.runnable_at(timestamp=15)
  assert 'p1' in p.runnable_at(timestamp=20)
  p.set_running('p1')
  p.add_failure('p1', timestamp=10)
  assert 'p1' not in p.runnable_at(timestamp=10)
  assert 'p1' not in p.runnable_at(timestamp=15)
  assert 'p1' in p.runnable_at(timestamp=20)
  assert 'p1' in p.runnable_at(timestamp=25)
  p.set_running('p1')
  p.add_failure('p1', timestamp=15)
  assert 'p1' not in p.runnable_at(timestamp=15)
  assert 'p1' not in p.runnable_at(timestamp=20)
  assert 'p1' not in p.runnable_at(timestamp=25)  # task past maximum failure limit
  assert 'p1' not in p.runnable_at(timestamp=30)


def test_task_waits():
  dt = p1(daemon=True, max_failures=0)
  p = TaskPlanner(empty_task(processes=[dt(name='d3', min_duration=3),
                                        dt(name='d5', min_duration=5),
                                        dt(name='d7', min_duration=7)]))
  assert p.runnable_at(timestamp=0) == _('d3', 'd5', 'd7')
  assert p.min_wait(timestamp=0) == TaskPlanner.INFINITY

  p.set_running('d3')
  p.add_success('d3', timestamp=0)
  assert p.runnable_at(timestamp=0) == _('d5', 'd7')
  assert p.waiting_at(timestamp=0) == _('d3')
  assert approx_equal(p.get_wait('d3', timestamp=0), 3)
  assert approx_equal(p.min_wait(timestamp=0), 3)
  assert approx_equal(p.min_wait(timestamp=1), 2)
  assert p.waiting_at(timestamp=3) == empty
  assert p.runnable_at(timestamp=3) == _('d3', 'd5', 'd7')

  p.set_running('d3')
  p.set_running('d7')
  p.add_success('d7', timestamp=1)
  assert approx_equal(p.min_wait(timestamp=2), 6)
  assert approx_equal(p.min_wait(timestamp=3), 5)
  p.add_success('d3', timestamp=3)
  assert approx_equal(p.min_wait(timestamp=3), 3)
  assert p.runnable_at(timestamp=2) == _('d5')
  assert p.runnable_at(timestamp=6) == _('d3', 'd5')
  assert p.runnable_at(timestamp=8) == _('d3', 'd5', 'd7')

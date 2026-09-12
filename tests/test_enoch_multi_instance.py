"""Concurrent local workflows; no live chat, model, or host-service calls."""

from __future__ import annotations

import multiprocessing
import os
from pathlib import Path
import tempfile
import unittest

from enoch.agent_identity import install_agent_identity, load_active_agent_identity
from enoch.app.epoch import StaleDaemonEpoch, begin_daemon_epoch, require_current_daemon_epoch
from enoch.config import read_section, write_section_value
from enoch.tasks.events import task_event_path
from enoch.workflows import LocalWorkflowEngine
from tests.test_enoch_agent_identity import _identity


def _instance_worker(root: Path, name: str, connection) -> None:
    try:
        epoch = begin_daemon_epoch(root, provider="multi-instance-test")
        workflow = LocalWorkflowEngine(root, epoch=epoch)
        connection.send(("ready", os.getpid(), epoch.generation))
        while True:
            command = connection.recv()
            if command == "quit":
                break
            if command == "run":
                workflow.enqueue(42, f"task for {name}")
                task = workflow.start_next()
                assert task is not None
                worker = f"worker-{name}"
                assert workflow.claim(task.id, worker, os.getpid()) is not None
                workflow.finalize(task.id, "completed", result=name, worker_id=worker)
                connection.send(("completed", workflow.inspect().history[-1].result))
            elif command == "check":
                try:
                    require_current_daemon_epoch(epoch, root)
                    connection.send(("current",))
                except StaleDaemonEpoch:
                    connection.send(("stale",))
    except Exception as error:
        connection.send(("error", repr(error)))
    finally:
        connection.close()


class EnochMultiInstanceTests(unittest.TestCase):
    def test_two_processes_keep_identity_config_workflow_and_epochs_independent(self) -> None:
        context = multiprocessing.get_context("spawn")
        processes = []
        connections = []
        with tempfile.TemporaryDirectory() as directory:
            first, second = Path(directory) / "work", Path(directory) / "life"
            try:
                for root, name in ((first, "work"), (second, "life")):
                    document = _identity()
                    document["identity"]["id"] = name
                    install_agent_identity(document, root)
                    write_section_value("instance-test", "owner", name, root)
                    parent, child = context.Pipe()
                    process = context.Process(target=_instance_worker, args=(root, name, child))
                    process.start()
                    child.close()
                    processes.append(process)
                    connections.append(parent)

                ready = [self._receive(connection) for connection in connections]
                self.assertEqual([item[0] for item in ready], ["ready", "ready"])
                self.assertNotEqual(ready[0][1], ready[1][1])
                self.assertEqual([item[2] for item in ready], [1, 1])
                for connection in connections:
                    connection.send("run")
                self.assertEqual(self._receive(connections[0]), ("completed", "work"))
                self.assertEqual(self._receive(connections[1]), ("completed", "life"))

                for root, name in ((first, "work"), (second, "life")):
                    self.assertEqual(load_active_agent_identity(root)["identity"]["id"], name)
                    self.assertEqual(read_section("instance-test", root)["owner"], name)
                    history = LocalWorkflowEngine(root).inspect().history
                    self.assertEqual([(task.id, task.result) for task in history], [(1, name)])
                self.assertNotEqual(task_event_path(first), task_event_path(second))

                # Taking over one installed agent must not fence its neighbor.
                begin_daemon_epoch(first, provider="replacement-test")
                for connection in connections:
                    connection.send("check")
                self.assertEqual(self._receive(connections[0]), ("stale",))
                self.assertEqual(self._receive(connections[1]), ("current",))
                for connection in connections:
                    connection.send("quit")
                for process in processes:
                    process.join(timeout=10)
                    self.assertEqual(process.exitcode, 0)
            finally:
                for process in processes:
                    if process.is_alive():
                        process.terminate()
                    process.join(timeout=10)
                for connection in connections:
                    connection.close()

    def _receive(self, connection):
        self.assertTrue(connection.poll(30), "Instance worker did not respond")
        result = connection.recv()
        self.assertNotEqual(result[0], "error", result)
        return result

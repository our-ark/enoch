from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import tomllib
import unittest


ROOT = Path(__file__).resolve().parents[1]


class EnochPortableInstallTests(unittest.TestCase):
    def test_reference_providers_share_the_core_contract_pin(self) -> None:
        root_metadata = _project_metadata(ROOT / "pyproject.toml")
        core_contract = _dependency(root_metadata["project"]["dependencies"], "our-ark-provider-kit")
        for package in ("github", "launchd", "systemd", "telegram"):
            metadata = _project_metadata(ROOT / "libraries" / package / "pyproject.toml")
            provider_contract = _dependency(
                metadata["project"]["dependencies"],
                "our-ark-provider-kit",
            )
            self.assertEqual(provider_contract, core_contract, package)

    def test_installed_core_completes_task_with_external_chat_and_vcs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            target = base / "site-packages"
            plugin = base / "portable-providers"
            body = base / "body"
            codex = base / "codex"
            target.mkdir()
            _write_provider_package(plugin)
            _write_fake_codex(codex)

            install = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "--quiet",
                    "--disable-pip-version-check",
                    "--no-deps",
                    "--no-build-isolation",
                    "--target",
                    str(target),
                    str(ROOT / "libraries" / "provider-kit"),
                    str(ROOT / "libraries" / "skill-catalog"),
                    str(ROOT),
                    str(plugin),
                ],
                cwd=base,
                env={**os.environ, "PIP_NO_INDEX": "1"},
                text=True,
                capture_output=True,
                check=False,
                timeout=120,
            )
            self.assertEqual(install.returncode, 0, install.stderr or install.stdout)

            environment = {
                **os.environ,
                "ENOCH_CODEX_BIN": str(codex),
                "ENOCH_PYTHON": sys.executable,
                "ENOCH_TEST_COMMAND": f'{sys.executable} -c "pass"',
                "PIP_NO_INDEX": "1",
                "PYTHONPATH": str(target),
                "PYTHONPYCACHEPREFIX": str(base / "pycache"),
            }
            completed = subprocess.run(
                [sys.executable, "-c", _INSTALLED_TASK_SCRIPT, str(body)],
                cwd=base,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
                timeout=120,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
            result = json.loads(completed.stdout.strip().splitlines()[-1])

        self.assertEqual(result["chat"], "portable-chat")
        self.assertEqual(result["vcs"], "portable-vcs")
        self.assertEqual(result["runtime"], "codex")
        self.assertEqual(result["forge"], "local")
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["branch_preserved"])
        self.assertFalse(result["workspace_exists"])
        self.assertTrue(result["result_committed"])


def _project_metadata(path: Path) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def _dependency(dependencies: list[str], name: str) -> str:
    matches = [dependency for dependency in dependencies if dependency.split()[0] == name]
    if len(matches) != 1:
        raise AssertionError(f"Expected exactly one {name} dependency, found {matches}")
    return matches[0]


def _write_provider_package(root: Path) -> None:
    root.mkdir()
    (root / "pyproject.toml").write_text(
        textwrap.dedent(
            """
            [project]
            name = "enoch-portable-test-providers"
            version = "0.0.1"
            requires-python = ">=3.11"

            [project.entry-points."enoch.providers"]
            "chat.portable" = "portable_providers:create_chat"
            "vcs.portable" = "portable_providers:create_vcs"

            [build-system]
            requires = ["setuptools"]
            build-backend = "setuptools.build_meta"

            [tool.setuptools]
            py-modules = ["portable_providers"]
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    (root / "portable_providers.py").write_text(
        textwrap.dedent(
            """
            from enoch.providers.vcs import GitVersionControlProvider


            class PortableChat:
                name = "portable-chat"
                provider_kind = "chat"

                def __init__(self):
                    self.sent = []

                @property
                def allowed_conversation_id(self):
                    return "portable-room"

                def receive(self, cursor=None):
                    return []

                def send_message(self, conversation_id, text):
                    self.sent.append((conversation_id, text))
                    return f"message-{len(self.sent)}"

                def edit_message(self, conversation_id, message_id, text):
                    return None

                def send_read_ack(self, conversation_id, message_id):
                    return None


            class PortableVcs(GitVersionControlProvider):
                name = "portable-vcs"


            def create_chat(root=None):
                return PortableChat()


            def create_vcs(root=None):
                return PortableVcs()
            """
        ).lstrip(),
        encoding="utf-8",
    )


def _write_fake_codex(path: Path) -> None:
    path.write_text(
        f"#!{sys.executable}\n"
        + textwrap.dedent(
            """
            import json
            from pathlib import Path
            import sys

            args = sys.argv[1:]
            cwd = Path(args[args.index("--cd") + 1])
            (cwd / "PORTABLE_RESULT.md").write_text(
                "Completed by installed Enoch.\\n",
                encoding="utf-8",
            )
            output = Path(args[args.index("--output-last-message") + 1])
            output.write_text("Completed portable task.", encoding="utf-8")
            print(json.dumps({"type": "thread.started", "thread_id": "portable-session"}))
            print(json.dumps({"type": "turn.completed", "usage": {}}))
            """
        ).lstrip(),
        encoding="utf-8",
    )
    path.chmod(0o755)


_INSTALLED_TASK_SCRIPT = textwrap.dedent(
    """
    import json
    from pathlib import Path
    import subprocess
    import sys

    from enoch.app.core import EnochApplication
    from enoch.identity import load_identity
    from enoch.providers import load_provider
    from enoch.tasks.queue import begin_next_task, enqueue_task, task_queue_status


    root = Path(sys.argv[1])
    root.mkdir()

    def git(*args):
        result = subprocess.run(
            ["git", *args], cwd=root, text=True, capture_output=True, check=False
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr or result.stdout)
        return result.stdout.strip()

    git("init", "-b", "main")
    git("config", "user.name", "Portable Enoch")
    git("config", "user.email", "portable@example.com")
    (root / ".gitignore").write_text(".enoch/\\n.agent/instance.yaml\\n", encoding="utf-8")
    (root / "README.md").write_text("portable body\\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-m", "initial")
    config = root / ".enoch" / "config.yaml"
    config.parent.mkdir()
    config.write_text(
        "providers:\\n  chat: portable\\n  vcs: portable\\n  forge: local\\n",
        encoding="utf-8",
    )

    chat = load_provider("chat", root)
    runtime = load_provider("runtime", root)
    vcs = load_provider("vcs", root)
    forge = load_provider("forge", root)
    app = EnochApplication(
        identity=load_identity(),
        root=root,
        client=chat,
        runtime=runtime,
        forge=forge,
    )
    queued = enqueue_task("portable-room", "Create PORTABLE_RESULT.md", root)
    running = begin_next_task(root)
    assert running is not None and running.id == queued.id
    app._run_task_job(running)
    completed = task_queue_status(root).history[-1]
    branch_preserved = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{completed.branch_name}"],
        cwd=root,
        check=False,
    ).returncode == 0
    result_committed = subprocess.run(
        ["git", "show", f"{completed.branch_name}:PORTABLE_RESULT.md"],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    ).returncode == 0
    print(json.dumps({
        "chat": chat.name,
        "vcs": vcs.name,
        "runtime": runtime.name,
        "forge": forge.name,
        "status": completed.status,
        "branch_preserved": branch_preserved,
        "workspace_exists": Path(completed.worktree_path).exists(),
        "result_committed": result_committed,
    }))
    """
)


if __name__ == "__main__":
    unittest.main()

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


@unittest.skipUnless(
    (ROOT / "libraries").is_dir() and (ROOT / ".github").is_dir(),
    "repository release tests are outside the inheritable agent body",
)
class EnochPortableInstallTests(unittest.TestCase):
    def test_ci_provisions_locked_build_backend_before_offline_wheel_test(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "tests.yml").read_text(
            encoding="utf-8"
        )
        install_command = (
            "python -m pip install --disable-pip-version-check --require-hashes "
            "-r .github/requirements/test-build.txt"
        )
        test_command = "python -m unittest discover -s tests"

        self.assertIn(install_command, workflow)
        self.assertLess(workflow.index(install_command), workflow.index(test_command))

        requirements = (
            ROOT / ".github" / "requirements" / "test-build.txt"
        ).read_text(encoding="utf-8")
        self.assertRegex(requirements, r"(?m)^setuptools==\d+\.\d+\.\d+ \\$\n")
        self.assertRegex(requirements, r"--hash=sha256:[0-9a-f]{64}$")
        locked_version = next(
            line.split("==", 1)[1].split()[0]
            for line in requirements.splitlines()
            if line.startswith("setuptools==")
        )
        self.assertGreaterEqual(
            tuple(int(part) for part in locked_version.split(".")),
            (83, 0, 0),
        )

    def test_release_metadata_matches_project_version(self) -> None:
        metadata = _project_metadata(ROOT / "pyproject.toml")
        version = metadata["project"]["version"]
        citation = (ROOT / "CITATION.cff").read_text(encoding="utf-8")

        self.assertIn(f"version: {version}", citation)
        self.assertIn(f"/releases/tag/v{version}", citation)
        self.assertTrue((ROOT / "docs" / "releases" / f"v{version}.md").is_file())

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

    def test_wheel_install_completes_profile_task_with_independent_packages(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            target = base / "site-packages"
            wheels = base / "wheels"
            chat_provider = base / "portable-chat"
            vcs_provider = base / "portable-vcs"
            profile_package = base / "portable-researcher-profile"
            body = base / "body"
            codex = base / "codex"
            target.mkdir()
            wheels.mkdir()
            _write_chat_provider_package(chat_provider)
            _write_vcs_provider_package(vcs_provider)
            _write_profile_package(profile_package)
            _write_fake_codex(codex)

            for project in (
                ROOT / "libraries" / "provider-kit",
                ROOT / "libraries" / "skill-catalog",
                ROOT,
                chat_provider,
                vcs_provider,
                profile_package,
            ):
                built = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "pip",
                        "wheel",
                        "--quiet",
                        "--disable-pip-version-check",
                        "--no-deps",
                        "--no-build-isolation",
                        "--wheel-dir",
                        str(wheels),
                        str(project),
                    ],
                    cwd=base,
                    env={**os.environ, "PIP_NO_INDEX": "1"},
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=120,
                )
                self.assertEqual(built.returncode, 0, built.stderr or built.stdout)

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
                    *(str(path) for path in sorted(wheels.glob("*.whl"))),
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
        self.assertEqual(result["enoch_version"], "0.2.0")
        self.assertEqual(result["chat_provider_version"], "0.0.1")
        self.assertEqual(result["vcs_provider_version"], "0.0.1")
        self.assertEqual(result["profile"], "researcher")
        self.assertEqual(result["profile_version"], "0.0.1")
        self.assertEqual(result["profile_trigger"], "/research")
        self.assertEqual(result["profile_context_source"], "profile:researcher")
        self.assertIn("Queued portable research task #1", result["profile_command_reply"])
        self.assertEqual(result["runtime_provider"], "codex")
        self.assertEqual(result["runtime_session_id"], "portable-session")
        self.assertEqual(result["runtime_completion_reason"], "completed")
        self.assertEqual(
            result["runtime_event_types"],
            ["thread.started", "turn.completed"],
        )
        self.assertGreater(result["startup_messages"], 0)
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


def _write_chat_provider_package(root: Path) -> None:
    root.mkdir()
    (root / "pyproject.toml").write_text(
        textwrap.dedent(
            """
            [project]
            name = "enoch-portable-chat-provider"
            version = "0.0.1"
            requires-python = ">=3.11"

            [project.entry-points."our_ark.providers"]
            "chat.portable" = "portable_chat:create_provider"

            [build-system]
            requires = ["setuptools"]
            build-backend = "setuptools.build_meta"

            [tool.setuptools]
            py-modules = ["portable_chat"]
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    (root / "portable_chat.py").write_text(
        textwrap.dedent(
            """
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


            def create_provider(root=None):
                return PortableChat()
            """
        ).lstrip(),
        encoding="utf-8",
    )


def _write_vcs_provider_package(root: Path) -> None:
    root.mkdir()
    (root / "pyproject.toml").write_text(
        textwrap.dedent(
            """
            [project]
            name = "enoch-portable-vcs-provider"
            version = "0.0.1"
            requires-python = ">=3.11"

            [project.entry-points."our_ark.providers"]
            "vcs.portable" = "portable_vcs:create_provider"

            [build-system]
            requires = ["setuptools"]
            build-backend = "setuptools.build_meta"

            [tool.setuptools]
            py-modules = ["portable_vcs"]
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    (root / "portable_vcs.py").write_text(
        textwrap.dedent(
            """
            from pathlib import Path
            import subprocess


            class PortableVcs:
                name = "portable-vcs"
                provider_kind = "vcs"

                def _git(self, args, root=None, *, check=True):
                    result = subprocess.run(
                        ["git", *args],
                        cwd=root,
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    if check and result.returncode != 0:
                        raise RuntimeError(result.stderr or result.stdout)
                    return result

                def current_branch(self, root=None):
                    return self._git(["branch", "--show-current"], root).stdout.strip()

                def is_clean(self, root=None):
                    return not self._git(["status", "--porcelain"], root).stdout.strip()

                def changed_files(self, root=None):
                    tracked = self._git(["diff", "--name-only", "HEAD"], root).stdout.splitlines()
                    untracked = self._git(
                        ["ls-files", "--others", "--exclude-standard"], root
                    ).stdout.splitlines()
                    return [path for path in [*tracked, *untracked] if path]

                def diff_summary(self, root=None):
                    return self._git(["diff", "--stat", "HEAD"], root).stdout.strip()

                def stage(self, files, root=None):
                    self._git(["add", "--", *files], root)

                def commit(self, message, root=None):
                    self._git(["commit", "-m", message], root)
                    return self.current_revision(root)

                def create_branch(self, branch, root=None, *, start_point=""):
                    args = ["switch", "-c", branch]
                    if start_point:
                        args.append(start_point)
                    self._git(args, root)

                def switch_branch(self, branch, root=None):
                    self._git(["switch", branch], root)

                def delete_branch(self, branch, root=None, *, force=False):
                    self._git(["branch", "-D" if force else "-d", branch], root)

                def branch_exists(self, branch, root=None):
                    return self._git(
                        ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
                        root,
                        check=False,
                    ).returncode == 0

                def task_base(self, root=None):
                    return self.authoritative_branch(root)

                def authoritative_branch(self, root=None):
                    return "main"

                def refresh_authoritative(self, root=None):
                    return ""

                def authoritative_revision(self, root=None):
                    return self.resolve_revision(self.authoritative_branch(root), root)

                def current_revision(self, root=None):
                    return self.resolve_revision("HEAD", root)

                def resolve_revision(self, revision, root=None):
                    result = self._git(["rev-parse", revision], root, check=False)
                    return result.stdout.strip() if result.returncode == 0 else ""

                def is_ancestor(self, revision, descendant, root=None):
                    return self._git(
                        ["merge-base", "--is-ancestor", revision, descendant],
                        root,
                        check=False,
                    ).returncode == 0

                def update_to_authoritative(self, root=None):
                    return "Already up to date."

                def restore_revision(self, revision, root=None):
                    self._git(["reset", "--hard", revision], root)

                def workspace_paths(self, root=None):
                    output = self._git(["worktree", "list", "--porcelain"], root).stdout
                    return tuple(
                        Path(line.removeprefix("worktree ")).resolve()
                        for line in output.splitlines()
                        if line.startswith("worktree ")
                    )

                def create_workspace(
                    self,
                    path,
                    branch,
                    root=None,
                    *,
                    start_point="",
                    create_branch=False,
                ):
                    args = ["worktree", "add"]
                    if create_branch:
                        args.extend(["-b", branch])
                    args.extend([str(path), start_point or branch])
                    self._git(args, root)

                def remove_workspace(self, path, root=None):
                    self._git(["worktree", "remove", str(path)], root)


            def create_provider(root=None):
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
            prompt = sys.stdin.read()
            if "Operate as a careful researcher." not in prompt:
                raise SystemExit("Installed profile context did not reach the task prompt.")
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


def _write_profile_package(root: Path) -> None:
    root.mkdir()
    (root / "pyproject.toml").write_text(
        textwrap.dedent(
            """
            [project]
            name = "enoch-portable-researcher-profile"
            version = "0.0.1"
            requires-python = ">=3.11"

            [project.entry-points."our_ark.profiles"]
            researcher = "portable_researcher:create_profile"

            [build-system]
            requires = ["setuptools"]
            build-backend = "setuptools.build_meta"

            [tool.setuptools]
            py-modules = ["portable_researcher"]
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    (root / "portable_researcher.py").write_text(
        textwrap.dedent(
            """
            from enoch.profiles import AgentProfile, CommandSpec


            def research(command):
                if not command.argument:
                    return "Use /research <topic>."
                job = command.enqueue_task(
                    "Create PORTABLE_RESULT.md after researching " + command.argument,
                    context="Use primary sources and preserve provenance.",
                )
                return f"Queued portable research task #{job.id}."


            def research_context(context):
                return "Operate as a careful researcher." if context.purpose == "task" else ""


            def create_profile(root=None):
                return AgentProfile(
                    name="researcher",
                    commands=(
                        CommandSpec(
                            name="research",
                            summary="queue portable research",
                            handler=research,
                        ),
                    ),
                    prompt_contributors=(research_context,),
                )
            """
        ).lstrip(),
        encoding="utf-8",
    )


_INSTALLED_TASK_SCRIPT = textwrap.dedent(
    """
    import json
    from importlib.metadata import version
    from pathlib import Path
    import subprocess
    import sys

    from enoch.app.core import EnochApplication
    from enoch.identity import load_identity
    from enoch.profiles import load_profile
    from enoch.providers import ChatEvent, load_provider
    from enoch.tasks.queue import begin_next_task, task_queue_status


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
        "providers:\\n  chat: portable\\n  vcs: portable\\n  forge: local\\n"
        "agent:\\n  profile: researcher\\n",
        encoding="utf-8",
    )

    chat = load_provider("chat", root)
    runtime = load_provider("runtime", root)
    vcs = load_provider("vcs", root)
    forge = load_provider("forge", root)
    profile = load_profile(root)
    app = EnochApplication(
        identity=load_identity(),
        root=root,
        client=chat,
        runtime=runtime,
        forge=forge,
        profile=profile,
    )
    app.notify_startup()
    app.handle_event(
        ChatEvent(
            cursor="profile-command",
            conversation_id="portable-room",
            message_id="profile-command",
            text="/research stable extension APIs",
        )
    )
    queued = task_queue_status(root).pending[-1]
    profile_command_reply = chat.sent[-1][1]
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
        "enoch_version": version("enoch"),
        "chat_provider_version": version("enoch-portable-chat-provider"),
        "vcs_provider_version": version("enoch-portable-vcs-provider"),
        "profile": profile.name,
        "profile_version": version("enoch-portable-researcher-profile"),
        "profile_trigger": completed.trigger,
        "profile_context_source": completed.context_source,
        "profile_command_reply": profile_command_reply,
        "runtime_provider": completed.runtime_provider,
        "runtime_session_id": completed.runtime_session_id,
        "runtime_completion_reason": completed.runtime_completion_reason,
        "runtime_event_types": completed.runtime_event_types,
        "startup_messages": len(chat.sent),
        "status": completed.status,
        "branch_preserved": branch_preserved,
        "workspace_exists": Path(completed.worktree_path).exists(),
        "result_committed": result_committed,
    }))
    """
)


if __name__ == "__main__":
    unittest.main()

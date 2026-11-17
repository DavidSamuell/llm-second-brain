"""Session hooks shipped by scripts/agent-plugin (SPEC-session-hooks), run as generated."""

import json
import os
import shutil
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest
from harness import REPO

from vault_core.repos import normalise_remote

pytestmark = pytest.mark.profile_local

URL = "https://docs-mcp.example.test/mcp"
ALLOW = "GitHub.com/Bookbot-Hive/*,git.example/Group/*,git.example:2222/Team/app"
NUDGE_EVERY = 10


def build(out: Path, *args: str) -> Path:
    result = subprocess.run(
        [str(REPO / "scripts" / "agent-plugin"), "--url", URL, "--out", str(out), *args],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return out / "docs-vault" / "hooks"


@pytest.fixture(scope="module")
def hooks(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build(tmp_path_factory.mktemp("plugin") / "market", "--capture-repos", ALLOW)


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    state = tmp_path / "tmp"
    state.mkdir()
    return {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "TMPDIR": str(state)}


def repo(tmp_path: Path, remote: str | None, name: str = "work", branch: str = "main") -> Path:
    path = tmp_path / name
    path.mkdir()
    subprocess.run(["git", "init", "-q", "-b", branch, str(path)], check=True)
    if remote is not None:
        subprocess.run(["git", "-C", str(path), "remote", "add", "origin", remote], check=True)
    return path


def run(
    hooks: Path, script: str, payload: object, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    data = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.run(
        ["bash", str(hooks / script)],
        input=data,
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=20,
    )


def context(result: subprocess.CompletedProcess[str], event: str) -> str | None:
    assert result.returncode == 0, result.stderr
    if not result.stdout.strip():
        return None
    output = json.loads(result.stdout)["hookSpecificOutput"]
    assert output["hookEventName"] == event
    text = output["additionalContext"]
    assert isinstance(text, str)
    assert 0 < len(text) <= 1500
    return text


def start(hooks: Path, cwd: Path, env: dict[str, str], source: str = "startup") -> str | None:
    payload = {"session_id": "s1", "cwd": str(cwd), "source": source}
    return context(run(hooks, "session-start.sh", payload, env), "SessionStart")


def prompt(hooks: Path, cwd: Path, env: dict[str, str], session: str = "s1") -> str | None:
    payload = {"session_id": session, "cwd": str(cwd), "prompt": "hi"}
    return context(run(hooks, "prompt-nudge.sh", payload, env), "UserPromptSubmit")


def repo_data(text: str) -> dict[str, str]:
    for line in text.splitlines():
        if '{"repo":' in line:
            return json.loads(line[line.index('{"repo":') :])
    raise ValueError("context contains no repository identity")


@pytest.mark.parametrize(
    ("remote", "identity"),
    [
        ("git@github.com:Bookbot-Hive/App.git", "github.com/bookbot-hive/app"),
        ("https://github.com/bookbot-hive/app", "github.com/bookbot-hive/app"),
        ("ssh://git@git.example/Group/Repo.git", "git.example/Group/Repo"),
        ("git@git.example:2222/Team/app.git", "git.example:2222/Team/app"),
    ],
)
def test_allowlisted_repo_gets_the_context_block(
    hooks: Path, env: dict[str, str], tmp_path: Path, remote: str, identity: str
) -> None:
    root = repo(tmp_path, remote, branch="feature/x")
    sub = root / "src" / "deep"
    sub.mkdir(parents=True)

    text = start(hooks, sub, env)  # run from below the git top-level

    assert text is not None
    assert repo_data(text) == {"repo": identity, "branch": "feature/x"}
    for needed in ("project_context", "docs-capture", ".docs-vault-ignore"):
        assert needed in text
    assert "DOCS_VAULT_CAPTURE=off" in text


@pytest.mark.parametrize(
    "remote",
    [
        "git@github.com:other-org/app.git",
        "https://git.example/group/Repo.git",  # non-GitHub paths keep their case
        "https://git.example/Group/Sub/Repo.git",  # * is one path segment
        "https://git.example:2222/Team/other.git",
        "https://github.com/bookbot-hive/$(id)",  # never shown, even though * would match
        "/srv/git/app.git",
    ],
)
def test_other_repos_get_nothing(
    hooks: Path, env: dict[str, str], tmp_path: Path, remote: str
) -> None:
    root = repo(tmp_path, remote)
    assert start(hooks, root, env) is None
    assert all(prompt(hooks, root, env) is None for _ in range(NUDGE_EVERY))


def test_folders_without_a_remote_or_git_get_nothing(
    hooks: Path, env: dict[str, str], tmp_path: Path
) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    assert start(hooks, repo(tmp_path, None), env) is None
    assert start(hooks, plain, env) is None
    assert start(hooks, tmp_path / "missing", env) is None


def test_unsafe_branch_is_left_out_not_interpreted(
    hooks: Path, env: dict[str, str], tmp_path: Path
) -> None:
    root = repo(tmp_path, "git@github.com:bookbot-hive/app.git", branch="x$(id)`id`")
    text = start(hooks, root, env)
    assert text is not None
    assert repo_data(text) == {"repo": "github.com/bookbot-hive/app"}
    assert "uid=" not in text


def test_opt_outs_silence_both_hooks(hooks: Path, env: dict[str, str], tmp_path: Path) -> None:
    root = repo(tmp_path, "git@github.com:bookbot-hive/app.git")
    sub = root / "pkg"
    sub.mkdir()

    for value in ("off", "OFF", "0", "false"):
        off = {**env, "DOCS_VAULT_CAPTURE": value}
        assert start(hooks, sub, off) is None
        assert all(prompt(hooks, sub, off, session=value) is None for _ in range(NUDGE_EVERY))

    (root / ".docs-vault-ignore").touch()
    assert start(hooks, sub, env) is None
    assert all(prompt(hooks, sub, env) is None for _ in range(NUDGE_EVERY))


def test_compaction_and_clear_reinject_with_a_capture_reminder(
    hooks: Path, env: dict[str, str], tmp_path: Path
) -> None:
    root = repo(tmp_path, "git@github.com:bookbot-hive/app.git")
    first = start(hooks, root, env)
    assert first is not None
    for source in ("compact", "clear"):
        again = start(hooks, root, env, source=source)
        assert again is not None
        assert again != first
        assert repo_data(again) == repo_data(first)


def test_nudge_every_tenth_prompt_per_session(
    hooks: Path, env: dict[str, str], tmp_path: Path
) -> None:
    root = repo(tmp_path, "git@github.com:bookbot-hive/app.git")
    a = [prompt(hooks, root, env, session="session-a") for _ in range(25)]
    b = [prompt(hooks, root, env, session="session-b") for _ in range(10)]

    assert [i + 1 for i, text in enumerate(a) if text] == [10, 20]
    assert [i + 1 for i, text in enumerate(b) if text] == [10]


def test_opt_outs_stop_nudges_mid_session(hooks: Path, env: dict[str, str], tmp_path: Path) -> None:
    root = repo(tmp_path, "git@github.com:bookbot-hive/app.git")
    for _ in range(NUDGE_EVERY - 1):
        assert prompt(hooks, root, env) is None
    off = {**env, "DOCS_VAULT_CAPTURE": "off"}
    assert prompt(hooks, root, off) is None  # would have been the 10th; not counted
    assert prompt(hooks, root, env) is not None  # the 10th counted prompt

    for _ in range(NUDGE_EVERY - 1):
        assert prompt(hooks, root, env) is None
    (root / ".docs-vault-ignore").touch()
    assert prompt(hooks, root, env) is None
    (root / ".docs-vault-ignore").unlink()
    assert prompt(hooks, root, env) is not None


def test_session_state_is_private_and_not_named_by_session_id(
    hooks: Path, env: dict[str, str], tmp_path: Path
) -> None:
    root = repo(tmp_path, "git@github.com:bookbot-hive/app.git")
    escape = "../../../escaped"
    for _ in range(NUDGE_EVERY):
        prompt(hooks, root, env, session=escape)

    folders = list(Path(env["TMPDIR"]).iterdir())
    assert [p.name for p in folders] == [f"docs-vault-hooks-{os.getuid()}"]
    assert stat.S_IMODE(folders[0].stat().st_mode) == 0o700
    files = list(folders[0].iterdir())
    assert len(files) == 1
    assert stat.S_IMODE(files[0].stat().st_mode) == 0o600
    assert "escaped" not in files[0].name
    assert not (tmp_path / "escaped").exists()


@pytest.mark.parametrize("kind", ["shared", "symlink"])
def test_unsafe_state_folder_means_no_nudge_and_no_writes(
    hooks: Path, env: dict[str, str], tmp_path: Path, kind: str
) -> None:
    root = repo(tmp_path, "git@github.com:bookbot-hive/app.git")
    folder = Path(env["TMPDIR"]) / f"docs-vault-hooks-{os.getuid()}"
    target = tmp_path / "elsewhere"
    target.mkdir()
    if kind == "shared":
        folder.mkdir()
        folder.chmod(0o777)
    else:
        folder.symlink_to(target)

    assert all(prompt(hooks, root, env) is None for _ in range(NUDGE_EVERY))
    assert list(target.iterdir()) == []
    if kind == "shared":
        assert list(folder.iterdir()) == []


def tools_only(tmp_path: Path, *names: str) -> str:
    """A PATH with just these commands, to simulate a machine missing the others."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in names:
        found = shutil.which(name)
        assert found is not None, name
        (bin_dir / name).symlink_to(found)
    return str(bin_dir)


@pytest.mark.parametrize("missing", ["jq", "git"])
def test_missing_tools_fail_open(
    hooks: Path, env: dict[str, str], tmp_path: Path, missing: str
) -> None:
    root = repo(tmp_path, "git@github.com:bookbot-hive/app.git")
    present = [t for t in ("bash", "git", "jq", "cat", "tr", "sed", "id") if t != missing]
    reduced = {**env, "PATH": tools_only(tmp_path, *present)}

    for script in ("session-start.sh", "prompt-nudge.sh"):
        result = run(hooks, script, {"session_id": "s", "cwd": str(root)}, reduced)
        assert (result.returncode, result.stdout, result.stderr) == (0, "", "")


@pytest.mark.parametrize("payload", ["", "not json", "[1, 2]", '{"cwd": 5, "session_id": []}'])
def test_bad_input_fails_open(hooks: Path, env: dict[str, str], payload: str) -> None:
    for script in ("session-start.sh", "prompt-nudge.sh"):
        result = run(hooks, script, payload, env)
        assert (result.returncode, result.stdout, result.stderr) == (0, "", "")


def test_broken_git_repo_fails_open(hooks: Path, env: dict[str, str], tmp_path: Path) -> None:
    root = repo(tmp_path, "git@github.com:bookbot-hive/app.git")
    (root / ".git" / "HEAD").write_text("garbage\n")
    (root / ".git" / "config").write_text("[broken\n")
    for script in ("session-start.sh", "prompt-nudge.sh"):
        result = run(hooks, script, {"session_id": "s", "cwd": str(root)}, env)
        assert (result.returncode, result.stdout) == (0, "")


def test_default_build_has_an_empty_allowlist_and_does_nothing(
    env: dict[str, str], tmp_path: Path
) -> None:
    hooks = build(tmp_path / "market")
    root = repo(tmp_path, "git@github.com:bookbot-hive/app.git")
    assert start(hooks, root, env) is None
    assert all(prompt(hooks, root, env) is None for _ in range(NUDGE_EVERY))
    assert not Path(env["TMPDIR"]).joinpath(f"docs-vault-hooks-{os.getuid()}").exists()


REMOTES = [
    "git@github.com:Bookbot-Hive/Foo.git",
    "ssh://git@github.com/Bookbot-Hive/Foo.git",
    "https://github.com/Bookbot-Hive/Foo.git/",
    "github.com/Bookbot-Hive/Foo",
    "https://user:password@github.com/Org/Repo",
    pytest.param("https://user[broken@github.com/org/repo", id="malformed-userinfo"),
    "HTTPS://GitHub.com/Org/Repo",
    "https://git.example:443/Group/Repo.git",
    "https://git.example:0443/Group/Repo.git",
    "git@git.example:Group/Sub/Repo.git",
    "ssh://git@git.example:2222/Group/Repo.git",
    "git.example:2222/Group/Repo",
    "ssh://git@[::1]:2222/Group/Repo.git",
    "git://git.example/Group/Repo",
    "http://localhost:80/Group/Repo",
    "https://git.example/Group/Re%2Dpo",
    "https://git.example/Group/Repo?",
    "  https://github.com/org/repo  ",
    "",
    "/home/user/repo",
    "./repo",
    "../repo",
    "~/repo",
    "org/repo",
    "file:///tmp/repo",
    "https://github.com/org/repo/tree/main",
    "https://github.com/org/../repo",
    "https://github.com/org/repo?token=x",
    "https://github.com/org/repo#fragment",
    "https://github.com/org/re%00po",
    "https://github.com/org/re%0Apo",
    "https://github.com/org/re%20po",
    "https://github.com:bad/org/repo",
    "https://github.com:99999/org/repo",
    "https://git.example/Group\\Repo",
    "ftp://git.example/Group/Repo",
]


@pytest.mark.parametrize("remote", REMOTES)
def test_remote_identity_matches_the_server(hooks: Path, remote: str) -> None:
    script = 'export LC_ALL=C; . "$1"; dv_normalise_remote "$2"'
    result = subprocess.run(
        ["bash", "-c", script, "_", str(hooks / "lib.sh"), remote],
        capture_output=True,
        text=True,
        check=False,
    )
    shell = result.stdout.strip() if result.returncode == 0 else None
    assert shell == normalise_remote(remote)


OMP_DRIVER = textwrap.dedent(
    """
    import { pathToFileURL } from "node:url";
    const [factoryPath, cwd, steps] = process.argv.slice(2);
    const { default: factory } = await import(pathToFileURL(factoryPath).href);
    const handlers = new Map();
    factory({ on: (name, fn) => handlers.set(name, fn) });
    const ctx = { cwd, sessionManager: { getSessionId: () => "omp-session" } };
    const results = [];
    const messages = [];
    for (const step of steps.split(",")) {
      if (step === "prompt" || step === "revive" || step === "discard") {
        const event = { reason: step === "revive" ? "revive" : "prompt", prompt: "hi" };
        const result = (await handlers.get("before_agent_start")(event, ctx)) ?? null;
        results.push(result);
        if (step === "prompt") {
          if (result?.message) messages.push({ role: "custom", ...result.message });
          await handlers.get("context")?.({ messages }, ctx);
        }
      } else {
        await handlers.get(step)({ type: step }, ctx);
      }
    }
    process.stdout.write(JSON.stringify(results));
    """
)


def run_omp(hooks: Path, cwd: Path, env: dict[str, str], steps: list[str]) -> list[str | None]:
    node = shutil.which("node")
    assert node is not None, "Node.js 22.18+ is needed to run the omp hook module"
    # Resolve version-manager shims before the hook's isolated HOME is installed.
    node = subprocess.run(
        [node, "-p", "process.execPath"], capture_output=True, text=True, check=True, timeout=10
    ).stdout.strip()
    driver = cwd.parent / "omp-driver.mjs"
    driver.write_text(OMP_DRIVER)
    result = subprocess.run(
        [node, str(driver), str(hooks / "pre" / "docs-vault.ts"), str(cwd), ",".join(steps)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    messages: list[str | None] = []
    for item in json.loads(result.stdout):
        if item is None:
            messages.append(None)
            continue
        message = item["message"]
        assert message["customType"] == "docs-vault-capture"
        assert message["display"] is False
        assert 0 < len(message["content"]) <= 1500
        messages.append(message["content"])
    return messages


def test_omp_module_injects_once_reinjects_after_compaction_and_nudges(
    hooks: Path, env: dict[str, str], tmp_path: Path
) -> None:
    root = repo(tmp_path, "git@github.com:bookbot-hive/app.git")
    steps = ["revive", "prompt", "prompt", "session_compact", "prompt"]
    steps += ["prompt"] * 7 + ["session_clear", "prompt", "prompt"]

    # One result per revive/prompt step; prompts are counted 1..12.
    out = run_omp(hooks, root, env, steps)

    assert len(out) == 13
    assert out[0] is None  # revive: nothing, not counted
    assert out[1] is not None
    assert repo_data(out[1]) == {"repo": "github.com/bookbot-hive/app", "branch": "main"}
    assert out[2] is None
    assert out[3] is not None  # after compaction
    assert repo_data(out[3]) == repo_data(out[1])
    assert out[3] != out[1]
    assert out[4:10] == [None] * 6
    nudge = out[10]  # prompt 10
    assert nudge is not None
    assert "docs-capture" in nudge
    assert "github.com/bookbot-hive/app" not in nudge
    assert out[11] is not None  # after clear
    assert repo_data(out[11]) == repo_data(out[1])
    assert out[12] is None


def test_omp_module_respects_opt_outs_and_other_repos(
    hooks: Path, env: dict[str, str], tmp_path: Path
) -> None:
    allowed = repo(tmp_path, "git@github.com:bookbot-hive/app.git")
    other = repo(tmp_path, "git@github.com:other/app.git", name="other")

    assert run_omp(hooks, other, env, ["prompt"] * NUDGE_EVERY) == [None] * NUDGE_EVERY
    off = {**env, "DOCS_VAULT_CAPTURE": "off"}
    assert run_omp(hooks, allowed, off, ["prompt"] * NUDGE_EVERY) == [None] * NUDGE_EVERY
    (allowed / ".docs-vault-ignore").touch()
    assert run_omp(hooks, allowed, env, ["prompt"] * NUDGE_EVERY) == [None] * NUDGE_EVERY


def test_omp_retries_a_discarded_startup_context(
    hooks: Path, env: dict[str, str], tmp_path: Path
) -> None:
    root = repo(tmp_path, "git@github.com:bookbot-hive/app.git")
    discarded, accepted, following = run_omp(hooks, root, env, ["discard", "prompt", "prompt"])
    assert discarded is not None
    assert accepted is not None
    assert repo_data(accepted) == repo_data(discarded)
    assert following is None

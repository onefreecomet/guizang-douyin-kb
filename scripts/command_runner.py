"""Run a CLI in its own process tree so command timeouts also kill descendants."""
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from process_tree import ProcessTree


def run_command(cmd, *, cwd=None, timeout=None, **kwargs):
    # Call sites use UTF-8 text output; these flags mirror subprocess.run's API.
    tree = ProcessTree([sys.executable, str(Path(__file__).resolve()), '--execute', *map(str, cmd)],
                       env=os.environ, cwd=cwd or Path.cwd(), combine_output=False)
    try:
        try:
            stdout, stderr = tree.proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            tree.terminate()
            stdout, stderr = tree.proc.communicate(timeout=5)
            raise subprocess.TimeoutExpired(cmd, timeout, output=stdout, stderr=stderr) from exc
        return subprocess.CompletedProcess(cmd, tree.proc.returncode, stdout, stderr)
    finally:
        if tree.proc.poll() is None:
            tree.terminate()
        tree.close()
        if tree.proc.stdout:
            tree.proc.stdout.close()
        if tree.proc.stderr:
            tree.proc.stderr.close()


if __name__ == '__main__':
    if os.environ.get('DY_START_GATE') == '1' and sys.stdin.readline().strip() != 'GO':
        sys.exit(1)
    if len(sys.argv) < 3 or sys.argv[1] != '--execute':
        sys.exit(2)
    try:
        child_env = dict(os.environ)
        child_env.pop('DY_START_GATE', None)
        child = subprocess.Popen(
            sys.argv[2:], stdin=subprocess.DEVNULL, env=child_env,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        sys.exit(child.wait())
    except OSError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(127)

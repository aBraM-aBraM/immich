#!/usr/bin/env python3
"""Upstream's .github/workflows/test.yml -> this fork's: the e2e suites under cight.

    python3 .github/cight/workflow.py < upstream-test.yml > .github/workflows/test.yml

Regenerated on every sync instead of kept as a patch, so upstream's edits to test.yml
(Renovate bumps, new jobs, new steps) never conflict. Line based on purpose: the output stays
upstream's text with a few marked edits, so `git diff` against upstream reads as what cight
changed. Fails loudly (exit 1) when upstream's shape moved and a rule no longer applies.

What changes:
- every job but pre-job and the two e2e jobs: `if: false` (they need immich-app's secrets);
- kept jobs: no org-token step; `github.token` instead (public repository, read only);
- e2e jobs: `id-token: write` (the job proves its repository to the cight cloud); x86 only
  (no Arm build of cight yet); full history, without old blobs (cight diffs against the
  merge base); cight installed; the docker stack started *inside* `cight run` together with
  the suite, so the containers get cight's agent. Pushes to main map, pull requests shadow.
"""
import re
import sys

KEEP = {"pre-job"}
E2E = {"e2e-tests-server-cli": ("1/2", "pnpm test"), "e2e-tests-web": ("2/2", "pnpm test:web")}
OFF = "    if: false # cight test repository: only the e2e jobs run (they need the immich-app organisation's secrets)"
TOKEN = "${{ steps.token.outputs.token }}"


class Shape(Exception):
    pass


def indent(line):
    return len(line) - len(line.lstrip(" ")) if line.strip() else None


def blocks(lines, level, start=0, end=None):
    """(start, end) of each item at `level`: `key:` lines (jobs) or `- ` lines (steps).
    Blank lines and comments between items belong to the item before."""
    end = len(lines) if end is None else end
    out, cur = [], None
    for i in range(start, end):
        n = indent(lines[i])
        if n is None or lines[i].lstrip().startswith("#") and n >= level:
            continue
        if n < level:
            break
        if n == level:
            if cur is not None:
                out.append((cur, i))
            cur = i
    if cur is not None:
        # trailing blank lines stay with the item; stop at the first line less indented
        j = cur + 1
        while j < end and (indent(lines[j]) is None or indent(lines[j]) > level):
            j += 1
        out.append((cur, j))
    return out


def jobs(lines):
    try:
        top = next(i for i, l in enumerate(lines) if l.rstrip() == "jobs:")
    except StopIteration:
        raise Shape("no top-level jobs:")
    out = {}
    for s, e in blocks(lines, 2, top + 1):
        m = re.match(r"^  ([\w.-]+):\s*$", lines[s])
        if not m:
            raise Shape(f"line {s + 1}: not a job key: {lines[s]!r}")
        out[m.group(1)] = (s, e)
    return out


def disable(job):
    """`if: false` in place of the job's own `if:`, or after its `name:` (or key) line."""
    for i, l in enumerate(job):
        if indent(l) == 4 and l.lstrip().startswith("if:"):
            # a multi-line `if: >` block: drop its continuation lines too
            j = i + 1
            while j < len(job) and indent(job[j]) is not None and indent(job[j]) > 4:
                j += 1
            return job[:i] + [OFF] + job[j:]
    at = next((i for i, l in enumerate(job) if indent(l) == 4 and l.lstrip().startswith("name:")), 0)
    return job[: at + 1] + [OFF] + job[at + 1 :]


def steps_of(job):
    """The job's steps as lists of lines, and the lines before them."""
    try:
        s = next(i for i, l in enumerate(job) if l.rstrip() == "    steps:")
    except StopIteration:
        raise Shape(f"job {job[0].strip()} has no steps:")
    items = blocks(job, 6, s + 1)
    if not items:
        raise Shape(f"job {job[0].strip()}: no steps")
    tail = job[items[-1][1] :]
    return job[: s + 1], [job[a:b] for a, b in items], tail


def field(step, key):
    """The value of `key:` at the step's own level (8 spaces, or after `- `)."""
    for l in step:
        m = re.match(r"^(?:      - |        )" + re.escape(key) + r":\s*(.*?)\s*$", l)
        if m:
            return m.group(1)
    return None


def script(step):
    """The step's `run:` as shell lines (a one-line value or a `|` block)."""
    for i, l in enumerate(step):
        m = re.match(r"^(?:      - |        )run:\s*(.*?)\s*$", l)
        if not m:
            continue
        v = m.group(1)
        if v not in ("|", "|-", ">", ">-"):
            return [v]
        if v.startswith(">"):
            raise Shape("folded run: block")
        body = []
        for b in step[i + 1 :]:
            if indent(b) is not None and indent(b) <= 8:
                break
            body.append(b[10:] if b.strip() else "")
        while body and not body[-1]:
            body.pop()
        return body
    return None


def drop_keys(step, keys):
    """Remove step-level keys (8 spaces) and their nested lines."""
    out, skip = [], False
    for l in step:
        n = indent(l)
        if skip and (n is None or n > 8):
            continue
        skip = False
        if n == 8 and re.match(r"^\s*(" + "|".join(map(re.escape, keys)) + r"):", l):
            skip = True
            continue
        out.append(l)
    while out and not out[-1].strip():
        out.pop()
    return out


def trimmed(step):
    """The step without its trailing blank lines, and those lines."""
    n = len(step)
    while n and not step[n - 1].strip():
        n -= 1
    return step[:n], step[n:]


def kept(job):
    head, steps, tail = steps_of(job)
    steps = [s for s in steps if field(s, "id") != "token"]
    lines = head + [l for s in steps for l in s] + tail
    return [l.replace(TOKEN, "${{ github.token }}") for l in lines]


def e2e(job, name):
    shard, command = E2E[name]
    head, steps, tail = steps_of(job)

    # permissions: id-token
    try:
        p = next(i for i, l in enumerate(head) if l.rstrip() == "    permissions:")
    except StopIteration:
        raise Shape(f"{name}: no permissions:")
    q = p + 1
    while q < len(head) and indent(head[q]) is not None and indent(head[q]) > 4:
        q += 1
    head.insert(q, "      id-token: write # cight: the job proves its repository to the cight cloud")

    # x86 runners only
    for i, l in enumerate(head):
        m = re.match(r"^(\s+runner:\s*)\[(.*)\]\s*$", l)
        if m:
            runners = [r.strip() for r in m.group(2).split(",") if "arm" not in r]
            if not runners:
                raise Shape(f"{name}: no x86 runner in the matrix")
            head[i] = f"{m.group(1)}[{', '.join(runners)}] # cight test repository: the cight client has no Arm build yet"
            break
    else:
        raise Shape(f"{name}: no runner matrix")

    steps = [s for s in steps if field(s, "id") != "token"]
    out = []
    docker, merged = None, False
    for s in steps:
        uses = field(s, "uses") or ""
        if uses.startswith("actions/checkout@"):
            s, gap = trimmed(s)
            s = [l for l in s if not re.match(r"^\s+token:", l)]
            s += [
                "          fetch-depth: 0 # cight: the merge base with main, for the change's diff",
                "          filter: blob:none # cight: history without old file contents (fetched when a diff needs them)",
            ]
            if not any(l.rstrip() == "        with:" for l in s):
                raise Shape(f"{name}: checkout has no with:")
            out += [s, ["", "      - name: Install cight", "        run: curl -fsSL https://cight.verrou.sh/install.sh | sh"] + gap]
            continue
        if field(s, "id") == "docker" and not merged:
            docker = s
            continue
        run = script(s)
        if docker is not None and not merged and run == [command]:
            out.append(merge(name, docker, s, shard, command) + trimmed(s)[1])
            merged = True
            continue
        out.append(s)
    if docker is None or not merged:
        raise Shape(f"{name}: no `id: docker` step followed by `run: {command}`")
    lines = head + [l for s in out for l in s] + tail
    lines = [l.replace(TOKEN, "${{ github.token }}") for l in lines]
    if TOKEN in "\n".join(lines) or "steps.token" in "\n".join(lines):
        raise Shape(f"{name}: a token reference is left")
    return lines


def merge(name, docker, test, shard, command):
    """The stack's step and the suite's as one step under `cight run`."""
    up = script(docker)
    if not up:
        raise Shape(f"{name}: the docker step has no run:")
    pipefail = field(docker, "shell") == "bash"
    if "'" in command:
        raise Shape("a quote in the suite's command")
    step = drop_keys(test, ["run", "if", "id", "shell"])
    # env: add cight's
    env = next((i for i, l in enumerate(step) if l.rstrip() == "        env:"), None)
    extra = [
        "          CIGHT_URL: ${{ vars.CIGHT_URL }}",
        f"          CIGHT_SHARD: {shard} # cight: the two e2e jobs map as two parts of one map",
    ]
    if env is None:
        step += ["        env:"] + extra
    else:
        e = env + 1
        while e < len(step) and indent(step[e]) is not None and indent(step[e]) > 8:
            e += 1
        step[e:e] = extra
    inner = "bash -eo pipefail" if pipefail else "bash -e"
    step += [
        "        id: docker",
        "        shell: bash",
        "        # cight: the stack starts inside `cight run`, so its containers get cight's agent; on a",
        "        # push the run maps, on a pull request it reports what cight would have run (shadow).",
        "        # The stack's script is upstream's own step, unchanged.",
        "        run: |",
        "          cat > \"$RUNNER_TEMP/cight-stack.sh\" <<'CIGHT_STACK'",
        *[("          " + l) if l else "" for l in up],
        "          CIGHT_STACK",
        f"          cight run -- bash -c '{inner} \"$RUNNER_TEMP/cight-stack.sh\" && {command}'",
        "        if: ${{ !cancelled() }}",
    ]
    if any("CIGHT_STACK" in l for l in up):
        raise Shape("the stack script contains the heredoc marker")
    return step


def transform(text):
    lines = text.split("\n")
    js = jobs(lines)
    missing = (KEEP | set(E2E)) - set(js)
    if missing:
        raise Shape(f"jobs not found: {sorted(missing)}")
    out = lines[: min(s for s, _ in js.values())]
    for name, (s, e) in js.items():
        job = lines[s:e]
        if name in E2E:
            out += e2e(job, name)
        elif name in KEEP:
            out += kept(job)
        else:
            out += disable(job)
    out += lines[max(e for _, e in js.values()) :]
    res = "\n".join(out)
    if not res.endswith("\n"):
        res += "\n"
    return res


if __name__ == "__main__":
    try:
        sys.stdout.write(transform(sys.stdin.read()))
    except Shape as e:
        sys.exit(f"workflow.py: upstream's test.yml changed shape: {e}")

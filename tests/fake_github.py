"""A small, stateful stand-in for the GitHub REST API and raw host.

It implements just the endpoints GitDb and Pixel use (contents, git data,
repository metadata, ``/user`` and ``raw.githubusercontent.com``-style file
serving with ``Range`` support), including GitHub's access rules: private
repositories answer 404 to anyone without access, and writes need push access.
"""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

ROLES = {"admin": 3, "write": 2, "read": 1}


@dataclass
class Repo:
    private: bool = True
    branch: str = "main"
    blobs: Dict[str, bytes] = field(default_factory=dict)
    trees: Dict[str, Dict[str, str]] = field(default_factory=dict)
    commits: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    heads: Dict[str, str] = field(default_factory=dict)
    roles: Dict[str, str] = field(default_factory=dict)  # login -> role


class FakeGitHub:
    def __init__(self) -> None:
        self.repos: Dict[str, Repo] = {}
        self.tokens: Dict[str, str] = {}  # token -> login
        self.lock = threading.RLock()
        self.requests: list = []
        self._counter = 0

    # ---------------------------------------------------------------- setup
    def add_user(self, token: str, login: str) -> None:
        self.tokens[token] = login

    def add_repo(self, name: str, *, private: bool = True, roles: Optional[Dict[str, str]] = None) -> Repo:
        repo = Repo(private=private, roles=dict(roles or {}))
        empty = self._tree_sha({})
        repo.trees[empty] = {}
        commit = self._commit_sha(empty, [])
        repo.commits[commit] = {"tree": empty, "parents": [], "message": "init"}
        repo.heads[repo.branch] = commit
        self.repos[name] = repo
        return repo

    def files(self, name: str) -> Dict[str, bytes]:
        repo = self.repos[name]
        tree = repo.trees[repo.commits[repo.heads[repo.branch]]["tree"]]
        return {path: repo.blobs[sha] for path, sha in tree.items()}

    # --------------------------------------------------------------- hashes
    @staticmethod
    def _blob_sha(data: bytes) -> str:
        return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()

    @staticmethod
    def _tree_sha(entries: Dict[str, str]) -> str:
        return hashlib.sha1(json.dumps(sorted(entries.items())).encode()).hexdigest()

    def _commit_sha(self, tree: str, parents: list) -> str:
        self._counter += 1
        return hashlib.sha1(f"{tree}{parents}{self._counter}{time.time()}".encode()).hexdigest()

    # ----------------------------------------------------------------- auth
    def login_for(self, request: Request) -> Optional[str]:
        header = request.headers.get("authorization", "")
        for prefix in ("Bearer ", "token "):
            if header.startswith(prefix):
                return self.tokens.get(header[len(prefix) :].strip(), "\0invalid")
        return None

    def role(self, repo: Repo, login: Optional[str]) -> int:
        if login is None:
            return 0
        return ROLES.get(repo.roles.get(login, ""), 0)


def build_app(state: FakeGitHub) -> FastAPI:
    app = FastAPI()

    def err(status: int, message: str) -> JSONResponse:
        return JSONResponse({"message": message}, status_code=status)

    def lookup(
        request: Request, owner: str, name: str, *, write: bool = False
    ) -> Tuple[Optional[Repo], Optional[JSONResponse]]:
        login = state.login_for(request)
        if login == "\0invalid":
            return None, err(401, "Bad credentials")
        repo = state.repos.get(f"{owner}/{name}")
        if repo is None:
            return None, err(404, "Not Found")
        role = state.role(repo, login)
        if repo.private and role < 1:
            return None, err(404, "Not Found")
        if write and role < 2:
            return None, err(403 if login else 401, "Resource not accessible")
        return repo, None

    def resolve(repo: Repo, ref: Optional[str]) -> Optional[str]:
        ref = ref or repo.branch
        if ref in repo.heads:
            return repo.heads[ref]
        if ref in repo.commits:
            return ref
        return None

    def tree_of(repo: Repo, commit: str) -> Dict[str, str]:
        return repo.trees[repo.commits[commit]["tree"]]

    def new_commit(repo: Repo, tree: Dict[str, str], message: str, parent: str) -> str:
        tree_sha = state._tree_sha(tree)
        repo.trees[tree_sha] = dict(tree)
        sha = state._commit_sha(tree_sha, [parent])
        repo.commits[sha] = {"tree": tree_sha, "parents": [parent], "message": message}
        return sha

    @app.middleware("http")
    async def track(request: Request, call_next: Any) -> Response:
        state.requests.append((request.method, request.url.path))
        return await call_next(request)

    @app.get("/user")
    def user(request: Request) -> Response:
        login = state.login_for(request)
        if login is None or login == "\0invalid":
            return err(401, "Bad credentials")
        return JSONResponse({"login": login, "avatar_url": None})

    @app.get("/repos/{owner}/{name}")
    def repo_info(request: Request, owner: str, name: str) -> Response:
        repo, error = lookup(request, owner, name)
        if error:
            return error
        assert repo is not None
        body: Dict[str, Any] = {
            "full_name": f"{owner}/{name}",
            "private": repo.private,
            "visibility": "private" if repo.private else "public",
            "default_branch": repo.branch,
        }
        login = state.login_for(request)
        if login:
            role = state.role(repo, login)
            body["permissions"] = {
                "admin": role >= 3,
                "maintain": role >= 3,
                "push": role >= 2,
                "triage": role >= 2,
                "pull": role >= 1 or not repo.private,
            }
        return JSONResponse(body)

    @app.get("/repos/{owner}/{name}/git/ref/heads/{branch:path}")
    def get_ref(request: Request, owner: str, name: str, branch: str) -> Response:
        repo, error = lookup(request, owner, name)
        if error:
            return error
        assert repo is not None
        if branch not in repo.heads:
            return err(404, "Not Found")
        return JSONResponse(
            {"ref": f"refs/heads/{branch}", "object": {"sha": repo.heads[branch], "type": "commit"}}
        )

    @app.patch("/repos/{owner}/{name}/git/refs/heads/{branch:path}")
    async def update_ref(request: Request, owner: str, name: str, branch: str) -> Response:
        repo, error = lookup(request, owner, name, write=True)
        if error:
            return error
        assert repo is not None
        body = await request.json()
        with state.lock:
            sha = body["sha"]
            if sha not in repo.commits:
                return err(422, "Object does not exist")
            if not body.get("force") and repo.heads.get(branch) not in repo.commits[sha]["parents"]:
                return err(422, "Update is not a fast forward")
            repo.heads[branch] = sha
        return JSONResponse({"ref": f"refs/heads/{branch}", "object": {"sha": sha}})

    @app.get("/repos/{owner}/{name}/git/commits/{sha}")
    def get_commit(request: Request, owner: str, name: str, sha: str) -> Response:
        repo, error = lookup(request, owner, name)
        if error:
            return error
        assert repo is not None
        commit = repo.commits.get(sha)
        if commit is None:
            return err(404, "Not Found")
        return JSONResponse(
            {"sha": sha, "tree": {"sha": commit["tree"]}, "parents": [{"sha": p} for p in commit["parents"]]}
        )

    @app.post("/repos/{owner}/{name}/git/commits")
    async def create_commit(request: Request, owner: str, name: str) -> Response:
        repo, error = lookup(request, owner, name, write=True)
        if error:
            return error
        assert repo is not None
        body = await request.json()
        with state.lock:
            sha = state._commit_sha(body["tree"], body["parents"])
            repo.commits[sha] = {
                "tree": body["tree"],
                "parents": list(body["parents"]),
                "message": body["message"],
            }
        return JSONResponse({"sha": sha}, status_code=201)

    @app.post("/repos/{owner}/{name}/git/blobs")
    async def create_blob(request: Request, owner: str, name: str) -> Response:
        repo, error = lookup(request, owner, name, write=True)
        if error:
            return error
        assert repo is not None
        body = await request.json()
        data = (
            base64.b64decode(body["content"])
            if body.get("encoding") == "base64"
            else body["content"].encode()
        )
        sha = state._blob_sha(data)
        repo.blobs[sha] = data
        return JSONResponse({"sha": sha}, status_code=201)

    @app.get("/repos/{owner}/{name}/git/blobs/{sha}")
    def get_blob(request: Request, owner: str, name: str, sha: str) -> Response:
        repo, error = lookup(request, owner, name)
        if error:
            return error
        assert repo is not None
        if sha not in repo.blobs:
            return err(404, "Not Found")
        if "raw" in request.headers.get("accept", ""):
            return Response(repo.blobs[sha])
        return JSONResponse(
            {"sha": sha, "encoding": "base64", "content": base64.b64encode(repo.blobs[sha]).decode()}
        )

    @app.post("/repos/{owner}/{name}/git/trees")
    async def create_tree(request: Request, owner: str, name: str) -> Response:
        repo, error = lookup(request, owner, name, write=True)
        if error:
            return error
        assert repo is not None
        body = await request.json()
        with state.lock:
            tree = dict(repo.trees.get(body.get("base_tree") or "", {}))
            for entry in body["tree"]:
                if entry.get("sha") is None:
                    if entry["path"] not in tree:
                        return err(422, f"path {entry['path']} does not exist")
                    tree.pop(entry["path"], None)
                else:
                    if entry["sha"] not in repo.blobs:
                        return err(422, "blob does not exist")
                    tree[entry["path"]] = entry["sha"]
            sha = state._tree_sha(tree)
            repo.trees[sha] = tree
        return JSONResponse({"sha": sha}, status_code=201)

    @app.get("/repos/{owner}/{name}/git/trees/{expression:path}")
    def get_tree(request: Request, owner: str, name: str, expression: str) -> Response:
        repo, error = lookup(request, owner, name)
        if error:
            return error
        assert repo is not None
        ref, _, prefix = expression.partition(":")
        commit = resolve(repo, ref)
        if commit is None:
            tree = repo.trees.get(ref)
            if tree is None:
                return err(404, "Not Found")
        else:
            tree = tree_of(repo, commit)
        prefix = prefix.strip("/")
        entries = []
        dirs = set()
        for path, sha in sorted(tree.items()):
            if prefix:
                if not path.startswith(prefix + "/"):
                    continue
                rel = path[len(prefix) + 1 :]
            else:
                rel = path
            parts = rel.split("/")
            for depth in range(1, len(parts)):
                dirs.add("/".join(parts[:depth]))
            entries.append({"path": rel, "type": "blob", "sha": sha, "mode": "100644"})
        if prefix and not entries:
            return err(404, "Not Found")
        entries.extend({"path": d, "type": "tree", "sha": "0" * 40, "mode": "040000"} for d in sorted(dirs))
        return JSONResponse({"sha": "tree", "tree": entries, "truncated": False})

    def contents_listing(tree: Dict[str, str], path: str) -> Optional[list]:
        prefix = path.strip("/") + "/" if path.strip("/") else ""
        seen: Dict[str, Dict[str, Any]] = {}
        for item, sha in tree.items():
            if not item.startswith(prefix):
                continue
            rest = item[len(prefix) :]
            head, sep, _ = rest.partition("/")
            full = prefix + head
            seen[head] = {
                "name": head,
                "path": full,
                "type": "dir" if sep else "file",
                "sha": None if sep else sha,
            }
        return list(seen.values()) if seen else None

    @app.get("/repos/{owner}/{name}/contents/{path:path}")
    def get_contents(
        request: Request, owner: str, name: str, path: str, ref: Optional[str] = None
    ) -> Response:
        repo, error = lookup(request, owner, name)
        if error:
            return error
        assert repo is not None
        commit = resolve(repo, ref)
        if commit is None:
            return err(404, "No commit found")
        tree = tree_of(repo, commit)
        if path in tree:
            data = repo.blobs[tree[path]]
            if "raw" in request.headers.get("accept", ""):
                return Response(data)
            return JSONResponse(
                {
                    "type": "file",
                    "name": path.rsplit("/", 1)[-1],
                    "path": path,
                    "sha": tree[path],
                    "encoding": "base64",
                    "content": base64.b64encode(data).decode(),
                    "size": len(data),
                }
            )
        listing = contents_listing(tree, path)
        if listing is None:
            return err(404, "Not Found")
        return JSONResponse(listing)

    @app.put("/repos/{owner}/{name}/contents/{path:path}")
    async def put_contents(request: Request, owner: str, name: str, path: str) -> Response:
        repo, error = lookup(request, owner, name, write=True)
        if error:
            return error
        assert repo is not None
        body = await request.json()
        branch = body.get("branch") or repo.branch
        with state.lock:
            head = repo.heads[branch]
            tree = dict(tree_of(repo, head))
            existing = tree.get(path)
            if existing is not None and not body.get("sha"):
                return err(422, 'Invalid request.\n\n"sha" wasn\'t supplied.')
            if existing is not None and body.get("sha") != existing:
                return err(409, f"{path} does not match {body.get('sha')}")
            if existing is None and body.get("sha"):
                return err(409, f"{path} does not match")
            data = base64.b64decode(body["content"])
            sha = state._blob_sha(data)
            repo.blobs[sha] = data
            tree[path] = sha
            commit = new_commit(repo, tree, body.get("message", ""), head)
            repo.heads[branch] = commit
        return JSONResponse(
            {"content": {"sha": sha, "path": path}, "commit": {"sha": commit}},
            status_code=201 if existing is None else 200,
        )

    @app.delete("/repos/{owner}/{name}/contents/{path:path}")
    async def delete_contents(request: Request, owner: str, name: str, path: str) -> Response:
        repo, error = lookup(request, owner, name, write=True)
        if error:
            return error
        assert repo is not None
        body = await request.json()
        branch = body.get("branch") or repo.branch
        with state.lock:
            head = repo.heads[branch]
            tree = dict(tree_of(repo, head))
            if path not in tree:
                return err(404, "Not Found")
            if body.get("sha") != tree[path]:
                return err(409, "sha does not match")
            tree.pop(path)
            commit = new_commit(repo, tree, body.get("message", ""), head)
            repo.heads[branch] = commit
        return JSONResponse({"commit": {"sha": commit}})

    @app.get("/raw/{owner}/{name}/{ref}/{path:path}")
    def raw(request: Request, owner: str, name: str, ref: str, path: str) -> Response:
        repo, error = lookup(request, owner, name)
        if error:
            return Response("404: Not Found", status_code=404)
        assert repo is not None
        commit = resolve(repo, ref)
        tree = tree_of(repo, commit) if commit else {}
        if path not in tree:
            return Response("404: Not Found", status_code=404)
        data = repo.blobs[tree[path]]
        etag = f'"{tree[path]}"'
        range_header = request.headers.get("range")
        if range_header and range_header.startswith("bytes="):
            start_text, _, end_text = range_header[6:].partition("-")
            try:
                if start_text:
                    start = int(start_text)
                    end = int(end_text) if end_text else len(data) - 1
                else:
                    start = max(0, len(data) - int(end_text))
                    end = len(data) - 1
            except ValueError:
                start, end = 0, len(data) - 1
            end = min(end, len(data) - 1)
            if start >= len(data) or start > end:
                return Response(status_code=416, headers={"Content-Range": f"bytes */{len(data)}"})
            return Response(
                data[start : end + 1],
                status_code=206,
                media_type="application/octet-stream",
                headers={
                    "Content-Range": f"bytes {start}-{end}/{len(data)}",
                    "Accept-Ranges": "bytes",
                    "ETag": etag,
                },
            )
        return Response(
            data, media_type="application/octet-stream", headers={"Accept-Ranges": "bytes", "ETag": etag}
        )

    return app


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class ServerThread:
    """Run an ASGI app with uvicorn on a background thread."""

    def __init__(self, app: Any, port: Optional[int] = None) -> None:
        self.port = port or free_port()
        config = uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning", lifespan="on")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> ServerThread:
        self.thread.start()
        deadline = time.time() + 10
        while not self.server.started:
            if time.time() > deadline:
                raise RuntimeError("server did not start")
            time.sleep(0.02)
        return self

    def __exit__(self, *exc: Any) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)

from __future__ import annotations

import io
from dataclasses import replace
from typing import Iterator

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from pixel.app import create_app
from pixel.config import Settings
from tests.fake_github import FakeGitHub, ServerThread, build_app

DATA_REPO = "charles2ke/pixel-data"
AUDIT_REPO = "charles2ke/pixel-audit"

OWNER = "owner-token"
FRIEND = "friend-token"
STRANGER = "stranger-token"
AUDIT = "audit-token"


@pytest.fixture
def github() -> Iterator[tuple[FakeGitHub, str]]:
    state = FakeGitHub()
    state.add_user(OWNER, "charles2ke")
    state.add_user(FRIEND, "friend")
    state.add_user(STRANGER, "stranger")
    state.add_user(AUDIT, "pixel-audit-bot")
    state.add_repo(DATA_REPO, private=True, roles={"charles2ke": "admin", "friend": "read"})
    state.add_repo(AUDIT_REPO, private=True, roles={"charles2ke": "admin", "pixel-audit-bot": "write"})
    with ServerThread(build_app(state)) as server:
        yield state, server.url


@pytest.fixture
def settings(github: tuple[FakeGitHub, str]) -> Settings:
    _, url = github
    return Settings(
        repo=DATA_REPO,
        api_url=url,
        raw_url=url + "/raw",
        audit_repo=AUDIT_REPO,
        audit_token=AUDIT,
        audit_flush_seconds=3600,
        max_upload_bytes=5 * 1024 * 1024,
    )


def make_client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings), headers={"X-Pixel": "1"})


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with make_client(settings) as test_client:
        yield test_client


@pytest.fixture
def fast_permissions(settings: Settings) -> Settings:
    return replace(settings, permission_ttl_seconds=0)


def jpeg(color: str = "red", size: tuple[int, int] = (64, 48)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, "JPEG")
    return buffer.getvalue()


def mp4(length: int = 4096) -> bytes:
    header = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"
    return header + bytes(range(256)) * (length // 256)


def sign_in(client: TestClient, token: str) -> None:
    response = client.post("/api/login", json={"token": token})
    assert response.status_code == 200, response.text

"""在线升级文件事务、Release信任边界、任务锁和管理员接口回归。"""
import asyncio
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import sys
import zipfile

import httpx
import pytest

from app.services.update_runtime import (
    RuntimeStore, UpdateError, UpdateBusy, OperationLock, atomic_json, read_json,
    extract_package, file_hashes, apply_files, package_version, validate_lock,
)
from app.services.updates import UpdateService, REPOSITORY, API_URL, release_info, checksum_for, trusted_url


def project(root, version="1.0.0"):
    root = Path(root)
    (root / "app").mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / "app/__init__.py").write_text(f'__version__ = "{version}"\n')
    (root / "app/main.py").write_text('from fastapi import FastAPI\napp = FastAPI()\n')
    (root / "scripts/run_server.py").write_text('# managed launcher\n')
    (root / "scripts/bootstrap_database.py").write_text('# forward schema\n')
    (root / "requirements.lock").write_text('fastapi==0.139.0\n')
    return root


def bundle(version="1.1.0", extras=None):
    output = io.BytesIO()
    files = {
        "app/__init__.py":f'__version__ = "{version}"\n',
        "app/main.py":'from fastapi import FastAPI\napp = FastAPI()\n',
        "scripts/run_server.py":"# launcher\n",
        "scripts/bootstrap_database.py":"# schema\n",
        "requirements.lock":"fastapi==0.139.0\n",
        ".env":"DO_NOT_COPY=1", "configs/app.yaml":"do_not_copy: true",
    }
    files.update(extras or {})
    with zipfile.ZipFile(output, "w") as z:
        for name, content in files.items():
            raw_name = "server-monitor-v" + version + "/" + name
            entry = zipfile.ZipInfo(raw_name)
            entry.filename = raw_name
            z.writestr(entry, content)
    return output.getvalue()


def metadata(payload, version="1.1.0"):
    tag = "v" + version
    name = f"server-monitor-{tag}.zip"
    checksum = f"{hashlib.sha256(payload).hexdigest()}  {name}\n".encode()
    prefix = f"https://github.com/{REPOSITORY}/releases/download/{tag}/"
    release = {"tag_name":tag,"name":"New release","body":"<img src=x onerror=alert(1)>","assets":[
        {"name":name,"size":len(payload),"browser_download_url":prefix+name},
        {"name":"SHA256SUMS.txt","size":len(checksum),"browser_download_url":prefix+"SHA256SUMS.txt"},
    ]}
    return release, checksum


def service(tmp_path, payload=None, checksum_override=None):
    root = project(tmp_path / "project")
    payload = payload or bundle()
    release, checksum = metadata(payload)
    requests = []
    def transport(request):
        requests.append(request)
        if str(request.url) == API_URL:
            return httpx.Response(200,json=release)
        if request.url.path.endswith("SHA256SUMS.txt"):
            return httpx.Response(200,content=checksum_override or checksum)
        return httpx.Response(200,content=payload,headers={"content-length":str(len(payload))})
    return UpdateService(root, "1.0.0", httpx.MockTransport(transport)), requests


async def finish(svc, action):
    await svc.start(action, "test-admin")
    await svc.task
    return svc.status()


async def test_check_download_ready_and_config_preservation(tmp_path):
    svc, _ = service(tmp_path)
    before = file_hashes(svc.store.root)
    checked = await finish(svc,"check")
    assert checked["state"] == "available" and checked["release"]["version"] == "1.1.0"
    result = await finish(svc,"download")
    assert result["state"] == "ready"
    staged = svc.store.home / "staging" / result["staged"]["id"]
    assert package_version(staged) == "1.1.0"
    assert not (staged/".env").exists() and not (staged/"configs").exists()
    assert file_hashes(svc.store.root) == before
    # 手动备份不能丢掉已下载包的可应用状态。
    assert (await finish(svc,"backup"))["state"] == "ready"


async def test_same_version_has_no_update_and_cannot_downgrade(tmp_path):
    svc,_ = service(tmp_path)
    svc.version="1.1.0"
    assert (await finish(svc,"check"))["state"] == "up_to_date"
    assert (await finish(svc,"download"))["state"] == "error"
    assert not (svc.store.home/"staged.json").exists()


async def test_hash_mismatch_never_marks_ready(tmp_path):
    svc,_ = service(tmp_path,checksum_override=b"0"*64+b"  server-monitor-v1.1.0.zip\n")
    result=await finish(svc,"download")
    assert result["state"] == "error" and "SHA256" in result["message"]
    assert not list((svc.store.home/"downloads").iterdir())


@pytest.mark.parametrize("url", ["http://github.com/a","https://github.com.evil.test/a","https://127.0.0.1/a","https://user:secret@github.com/a","https://github.com:8443/a"])
def test_untrusted_download_url_rejected(url):
    assert not trusted_url(url)


def test_release_requires_exact_assets_and_semantic_versions():
    data,_=metadata(bundle("1.10.0"),"1.10.0")
    assert release_info(data,"1.9.0")["has_update"]
    data["assets"][0]["browser_download_url"]="https://github.com/other/repo/releases/download/v1.10.0/file.zip"
    with pytest.raises(UpdateError):release_info(data,"1.0.0")


async def test_cross_host_redirect_does_not_send_token(tmp_path,monkeypatch):
    svc,_=service(tmp_path)
    seen=[]
    monkeypatch.setenv("GITHUB_TOKEN","fixture-only")
    def transport(request):
        seen.append(request)
        if request.url.host == "api.github.com":
            return httpx.Response(302,headers={"location":"https://release-assets.githubusercontent.com/asset"})
        return httpx.Response(200,content=b"safe")
    svc.transport=httpx.MockTransport(transport)
    assert await svc._fetch(API_URL,100)==b"safe"
    assert seen[0].headers["authorization"]=="Bearer fixture-only"
    assert "authorization" not in seen[1].headers


async def test_untrusted_redirect_not_requested(tmp_path):
    svc,_=service(tmp_path)
    seen=[]
    def transport(request):
        seen.append(request)
        return httpx.Response(302,headers={"location":"https://untrusted.invalid/file"})
    svc.transport=httpx.MockTransport(transport)
    with pytest.raises(UpdateError):await svc._fetch(API_URL,100)
    assert len(seen)==1


def test_checksum_must_bind_exact_filename():
    with pytest.raises(UpdateError):checksum_for("a"*64+"  another.zip", "wanted.zip")
    with pytest.raises(UpdateError):checksum_for(("a"*64+"  wanted.zip\n")*2,"wanted.zip")


@pytest.mark.parametrize("path", ["../escape.py","app/../../escape.py","app/C:/escape.py","app/CON.py","app/dir\\escape.py"])
def test_archive_paths_rejected(tmp_path,path):
    archive=tmp_path/"input.zip"
    archive.write_bytes(bundle(extras={path:"bad"}))
    with pytest.raises(UpdateError):extract_package(archive,tmp_path/"stage","1.1.0")
    assert not (tmp_path/"escape.py").exists()


def test_archive_symlink_and_version_mismatch(tmp_path):
    archive=tmp_path/"input.zip"
    with zipfile.ZipFile(archive,"w") as z:
        entry=zipfile.ZipInfo("root/app/link")
        entry.external_attr=(stat.S_IFLNK | 0o777)<<16
        z.writestr(entry,"../../.env")
    with pytest.raises(UpdateError):extract_package(archive,tmp_path/"s1")
    archive.write_bytes(bundle())
    with pytest.raises(UpdateError):extract_package(archive,tmp_path/"s2","1.2.0")


@pytest.mark.parametrize("line", ["--index-url https://bad.invalid", "-r other.txt", "pkg @ https://bad.invalid/pkg.whl", "pkg>=1.0", "pkg==1.*"])
def test_dependency_options_and_unpinned_versions_rejected(tmp_path,line):
    root=project(tmp_path/"project")
    (root/"requirements.lock").write_text(line)
    with pytest.raises(UpdateError):validate_lock(root)


def test_backup_restore_preserves_secrets_data_and_removes_new_code(tmp_path):
    root=project(tmp_path/"project")
    (root/".env").write_text("secret")
    (root/"configs").mkdir();(root/"configs/app.yaml").write_text("mine")
    (root/"logs").mkdir();(root/"logs/live.log").write_text("log")
    store=RuntimeStore(root)
    original=file_hashes(root)
    backup=store.backup()
    new=project(tmp_path/"new","1.1.0")
    (new/"app/new.py").write_text("extra=1")
    apply_files(root,new,original)
    assert package_version(root)=="1.1.0"
    store.restore(backup["name"],file_hashes(root))
    assert file_hashes(root)==original
    assert (root/".env").read_text()=="secret"
    assert (root/"configs/app.yaml").read_text()=="mine"
    assert (root/"logs/live.log").read_text()=="log"
    with zipfile.ZipFile(store.home/"backups"/backup["name"]/"code.zip") as z:
        assert ".env" not in z.namelist()


def test_operation_lock_exclusive_and_released(tmp_path):
    with OperationLock(tmp_path/"operation.lock"):
        with pytest.raises(UpdateBusy):
            with OperationLock(tmp_path/"operation.lock"):pass
    with OperationLock(tmp_path/"operation.lock"):pass


async def test_background_task_does_not_accept_competing_action(tmp_path):
    svc,_=service(tmp_path)
    started,release=asyncio.Event(),asyncio.Event()
    async def slow():
        started.set();await release.wait()
        svc.store.status("up_to_date","test",100)
    svc.check=slow
    await svc.start("check")
    await started.wait()
    with pytest.raises(UpdateBusy):await svc.start("backup")
    release.set();await svc.task
    assert (await finish(svc,"backup"))["backups"]


async def test_interrupted_download_state_can_be_retried(tmp_path):
    svc,_=service(tmp_path)
    svc.store.status("downloading","previous process")
    assert svc.status()["state"]=="error"
    assert (await finish(svc,"check"))["state"]=="available"


def test_mutating_operations_need_live_manager(tmp_path):
    svc,_=service(tmp_path)
    with pytest.raises(UpdateError):svc.request_operation("restart","admin")
    assert not (svc.store.home/"command.json").exists()


@pytest.mark.parametrize("route",["check","download","backup","apply","rollback","restart"])
async def test_update_endpoints_require_login(route):
    from test_routes import make_client
    async with make_client() as client:
        assert (await client.post('/api/system/update/'+route,json={"backup":"test"})).status_code==401


async def test_write_requires_same_origin_and_custom_header(tmp_path,monkeypatch):
    from test_routes import make_authed_client
    from app.routes import updates
    svc,_=service(tmp_path)
    monkeypatch.setattr(updates,"get_update_service",lambda:svc)
    async with make_authed_client(monkeypatch) as client:
        assert (await client.post('/api/system/update/check')).status_code==403
        assert (await client.post('/api/system/update/check',headers={'X-Update-Request':'1','Origin':'https://elsewhere.invalid'})).status_code==403
        response=await client.post('/api/system/update/check',headers={'X-Update-Request':'1'})
        assert response.status_code==202
        await svc.task
        response=await client.get('/api/system/update/status')
        assert response.json()['state']=='available'

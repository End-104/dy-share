# -*- coding: utf-8 -*-
"""
push_to_github.py —— 不依赖 git 二进制的整仓推送工具
用 GitHub Git Data API 把当前目录的文件一次性写成仓库的初始提交。

用法：
    python push_to_github.py --dry-run
    python push_to_github.py --token ghp_xxx
    set GITHUB_TOKEN=ghp_xxx && python push_to_github.py

参数：
    --owner   仓库所有者，默认 End-104
    --repo    仓库名，默认 dy-share
    --branch  分支名，默认 main
    --token   PAT；缺省读环境变量 GITHUB_TOKEN，再缺省读同目录 .github_token 文件
    --dry-run 只列出将要上传的文件，不写远端
    --message 提交信息
"""

import argparse
import base64
import concurrent.futures as futures
import fnmatch
import json
import os
import sys
import time
import urllib.error
import urllib.request

API = "https://api.github.com"
ROOT = os.path.dirname(os.path.abspath(__file__))

EXCLUDE_DIRS = {
    ".git", ".venv312", "venv", ".venv", "__pycache__",
    ".idea", ".vscode", ".mypy_cache", ".pytest_cache", "node_modules",
}
EXCLUDE_GLOBS = [
    "*.pyc", "*.pyo", "*.pyd.tmp", "_deleted_*.zip", "*.7z",
    ".github_token", "push_to_github.py.bak",
    "*.log", "push.log", "_verify_remote.py",
    # 一次性探针脚本：里面写死了 app_id/app_secret，只在本机跑，不进远端
    "_peek_*.py", "_set_state.py",
    # 运行期互斥锁：intake.lock / order_<sn>.lock 记的是本机 pid，推到远端后换台机器
    # 拉下来会被当成"已有实例在跑"，等于给新机器种一个假锁
    "*.lock",
    # SQLite 运行时日志：跟 used_devices.db 一起上远端会形成半写状态，恢复时互相打架
    "*.db-wal", "*.db-shm", "*.db-journal",
]
# 超过 GitHub 单文件硬上限（100MB）直接跳过并报告
SINGLE_FILE_LIMIT = 100 * 1024 * 1024


def iter_files(root):
    """遍历待上传文件，return [(绝对路径, 仓库内相对路径, 字节数)]"""
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDE_DIRS)
        for name in sorted(filenames):
            if any(fnmatch.fnmatch(name, g) for g in EXCLUDE_GLOBS):
                continue
            abs_path = os.path.join(dirpath, name)
            rel_path = os.path.relpath(abs_path, root).replace("\\", "/")
            try:
                size = os.path.getsize(abs_path)
            except OSError:
                continue
            out.append((abs_path, rel_path, size))
    return out


class GitHub:
    def __init__(self, token):
        self.token = token

    def call(self, method, path, payload=None, retries=3):
        url = path if path.startswith("http") else API + path
        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
        last_err = None
        for attempt in range(retries):
            req = urllib.request.Request(url, data=body, method=method)
            req.add_header("Accept", "application/vnd.github+json")
            req.add_header("X-GitHub-Api-Version", "2022-11-28")
            req.add_header("User-Agent", "dy-share-pusher")
            if self.token:
                req.add_header("Authorization", "Bearer " + self.token)
            if body is not None:
                req.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(req, timeout=90) as resp:
                    raw = resp.read()
                    return resp.status, (json.loads(raw) if raw else {})
            except urllib.error.HTTPError as e:
                raw = e.read()
                try:
                    detail = json.loads(raw)
                except Exception:
                    detail = {"raw": raw[:400].decode("utf-8", "replace")}
                # 404 在"探测 ref 是否存在"时是正常结果，直接交给调用方判断
                if e.code in (404, 409, 422):
                    return e.code, detail
                if e.code in (500, 502, 503, 504):
                    last_err = (e.code, detail)
                    time.sleep(2 + attempt * 3)
                    continue
                return e.code, detail
            except Exception as e:  # 网络层
                last_err = ("net", str(e))
                time.sleep(2 + attempt * 3)
        return last_err[0], last_err[1]


def make_blob(gh, owner, repo, abs_path):
    with open(abs_path, "rb") as f:
        data = f.read()
    payload = {"content": base64.b64encode(data).decode("ascii"), "encoding": "base64"}
    status, resp = gh.call("POST", f"/repos/{owner}/{repo}/git/blobs", payload)
    if status not in (200, 201):
        raise RuntimeError(f"blob 失败 {status}: {resp}")
    return resp["sha"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--owner", default="End-104")
    ap.add_argument("--repo", default="dy-share")
    ap.add_argument("--branch", default="main")
    ap.add_argument("--token", default=None)
    ap.add_argument("--message", default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    token = args.token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        token_file = os.path.join(ROOT, ".github_token")
        if os.path.exists(token_file):
            with open(token_file, "r", encoding="utf-8") as f:
                token = f.read().strip()

    files = iter_files(ROOT)
    total = sum(s for _, _, s in files)
    print(f"[清单] 待上传 {len(files)} 个文件，合计 {total/1024/1024:.2f} MB")
    big = [(p, s) for _, p, s in files if s > 5 * 1024 * 1024]
    for p, s in sorted(big, key=lambda x: -x[1]):
        print(f"       大文件 {s/1024/1024:7.2f} MB  {p}")

    if args.dry_run:
        for _, rel, size in files:
            print(f"{size:12d}  {rel}")
        print("[完成] dry-run，未写远端")
        return 0

    if not token:
        print("[缺少凭据] 需要 PAT：--token ghp_xxx 或环境变量 GITHUB_TOKEN")
        return 2

    ok = [(a, r, s) for a, r, s in files if s < SINGLE_FILE_LIMIT]
    skipped = [(r, s) for _, r, s in files if s >= SINGLE_FILE_LIMIT]
    for r, s in skipped:
        print(f"[跳过] 超过 100MB 限制：{r} ({s/1024/1024:.1f} MB)")

    gh = GitHub(token)
    status, me = gh.call("GET", "/user")
    if status != 200:
        print(f"[鉴权失败] {status} {me}")
        return 3
    print(f"[鉴权] {me.get('login')}")

    status, repo_info = gh.call("GET", f"/repos/{args.owner}/{args.repo}")
    if status != 200:
        print(f"[仓库不可访问] {status} {repo_info}")
        return 4
    print(f"[仓库] {repo_info['full_name']} 默认分支 {repo_info.get('default_branch')}")

    # 0) 空仓库无法直接调 Git Data API（blob/tree 均返回 409 Git Repository is empty），
    #    所以先用 Contents API 写入一个文件，把第一条提交造出来，拿到父提交后再走后续流程
    status, branches = gh.call("GET", f"/repos/{args.owner}/{args.repo}/branches?per_page=1")
    if status == 200 and not branches:
        seed_path = ".gitignore"
        seed_content = ""
        if os.path.exists(os.path.join(ROOT, seed_path)):
            with open(os.path.join(ROOT, seed_path), "rb") as f:
                seed_content = base64.b64encode(f.read()).decode("ascii")
        else:
            seed_content = base64.b64encode(b"# bootstrap\n").decode("ascii")
        status, seed = gh.call(
            "PUT", f"/repos/{args.owner}/{args.repo}/contents/{seed_path}",
            {"message": "chore: 初始化仓库", "content": seed_content, "branch": args.branch},
        )
        if status not in (200, 201):
            print(f"[初始化失败] {status} {seed}")
            return 9
        print(f"[初始化] 空仓库已建立首条提交 {seed['commit']['sha'][:12]}")

    # 1) 并行上传 blob
    entries = []
    errors = []

    def worker(item):
        abs_path, rel, size = item
        sha = make_blob(gh, args.owner, args.repo, abs_path)
        return {"path": rel, "mode": "100644", "type": "blob", "sha": sha}, size

    done = 0
    with futures.ThreadPoolExecutor(max_workers=8) as pool:
        tasks = {pool.submit(worker, it): it for it in ok}
        for fut in futures.as_completed(tasks):
            item = tasks[fut]
            try:
                entry, size = fut.result()
                entries.append(entry)
                done += size
                print(f"[blob {len(entries):>3}/{len(ok)}] {item[1]}  "
                      f"{size/1024:8.1f} KB  累计 {done/1024/1024:.2f} MB")
            except Exception as e:
                errors.append((item[1], str(e)))
                print(f"[blob 失败] {item[1]}: {e}")

    if errors:
        print(f"[中止] {len(errors)} 个文件上传失败")
        for r, e in errors:
            print("   ", r, e)
        return 5

    # 2) 建树
    status, tree = gh.call("POST", f"/repos/{args.owner}/{args.repo}/git/trees",
                           {"tree": entries})
    if status not in (200, 201):
        print(f"[建树失败] {status} {tree}")
        return 6
    tree_sha = tree["sha"]
    print(f"[tree] {tree_sha}")

    # 3) 是否已有目标分支，决定提交父节点
    status, ref = gh.call("GET", f"/repos/{args.owner}/{args.repo}/git/ref/heads/{args.branch}")
    parents = []
    if status == 200 and isinstance(ref, dict) and ref.get("object"):
        parents = [ref["object"]["sha"]]
        print(f"[分支] {args.branch} 已存在，父提交 {parents[0][:12]}")
    else:
        print(f"[分支] {args.branch} 不存在，创建初始提交")

    msg = args.message or "chore: 首次导入 dy_share 全部程序文件"
    status, commit = gh.call("POST", f"/repos/{args.owner}/{args.repo}/git/commits",
                             {"message": msg, "tree": tree_sha, "parents": parents})
    if status not in (200, 201):
        print(f"[提交失败] {status} {commit}")
        return 7
    commit_sha = commit["sha"]
    print(f"[commit] {commit_sha}")

    # 4) 更新/创建 ref
    if parents:
        status, out = gh.call("PATCH", f"/repos/{args.owner}/{args.repo}/git/refs/heads/{args.branch}",
                              {"sha": commit_sha, "force": False})
    else:
        status, out = gh.call("POST", f"/repos/{args.owner}/{args.repo}/git/refs",
                              {"ref": f"refs/heads/{args.branch}", "sha": commit_sha})
    if status not in (200, 201):
        print(f"[更新分支失败] {status} {out}")
        return 8

    status, repo_info = gh.call("GET", f"/repos/{args.owner}/{args.repo}")
    print(f"[完成] https://github.com/{args.owner}/{args.repo}/commit/{commit_sha}")
    print(f"[文件数] {len(entries)}    [分支] {args.branch}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

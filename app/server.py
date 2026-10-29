"""溯文 SeekLit —— HTTP 服务（纯标准库实现）。

为什么不用 FastAPI / Flask？
    部署机的出网带宽实测只有 15–25 KB/s，而 fastapi 0.162 的依赖树里
    包含 opentelemetry-api、annotated-doc、starlette、pydantic-core 等
    数十个包，在这种带宽下 pip 反复读超时、装不上。
    本项目的接口面很小（十余个 JSON 端点 + 三个静态文件），
    用 `http.server.ThreadingHTTPServer` 自己写一个 40 行的路由器，
    就能把 Web 层的外部依赖降到 **0**，`pymysql` + `numpy` 之外什么都不需要装。
    这直接服务于「可运行性」：clone 下来就能跑，不会因为网络装不上依赖。

端点契约与前端 app/static/app.js 严格对应，所有响应都是 JSON。
"""
from __future__ import annotations

import json
import logging
import mimetypes
import os
import re
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlparse

from . import config, db, embedder, ingest, schema, search
from .snapshot import build_snapshot, list_preset_queries

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("seeklit.server")

STATIC_DIR = Path(__file__).resolve().parent / "static"
APP_VERSION = "1.0.0"


# ---------------------------------------------------------------------------
# 极简路由器
# ---------------------------------------------------------------------------
class Route:
    __slots__ = ("method", "regex", "handler", "auth", "summary")

    def __init__(self, method: str, pattern: str, handler: Callable,
                 auth: bool = False, summary: str = ""):
        self.method = method.upper()
        self.regex = re.compile(f"^{pattern}$")
        self.handler = handler
        self.auth = auth
        self.summary = summary


_ROUTES: list[Route] = []


def route(method: str, pattern: str, auth: bool = False, summary: str = ""):
    """把函数注册成一个端点。函数签名 (ctx, **path_params) -> (status, payload)。"""
    def deco(fn: Callable):
        text = summary
        if not text and fn.__doc__:
            text = fn.__doc__.strip().splitlines()[0].strip()
        _ROUTES.append(Route(method, pattern, fn, auth, text))
        return fn
    return deco


class Ctx:
    """一次请求的上下文：查询串、JSON 体、头。"""

    def __init__(self, query: dict[str, list[str]], body: Any, headers):
        self.query = query
        self.body = body if isinstance(body, dict) else {}
        self.headers = headers

    def q(self, key: str, default: Any = None) -> Any:
        vals = self.query.get(key)
        return vals[0] if vals else default

    def q_int(self, key: str, default: int | None = None) -> int | None:
        raw = self.q(key)
        if raw is None or raw == "":
            return default
        try:
            return int(raw)
        except (TypeError, ValueError):
            return default

    def token(self) -> str:
        hdr = self.headers.get("X-SeekLit-Token") or ""
        return (hdr or self.q("token") or "").strip()


class ApiError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


# ---------------------------------------------------------------------------
# 请求体校验（替代 pydantic，保持同样的宽松/严格程度）
# ---------------------------------------------------------------------------
def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, (list, tuple, set)):
        return [str(v) for v in value if v not in (None, "")]
    return [str(value)]


def parse_filters(raw: Any) -> search.Filters:
    raw = raw if isinstance(raw, dict) else {}
    page_from = raw.get("page_from")
    page_to = raw.get("page_to")
    return search.Filters(
        kinds=_as_str_list(raw.get("kinds")),
        langs=_as_str_list(raw.get("langs")),
        doc_ids=_as_str_list(raw.get("doc_ids")),
        tag=str(raw.get("tag") or ""),
        page_from=int(page_from) if page_from not in (None, "") else None,
        page_to=int(page_to) if page_to not in (None, "") else None,
    )


def _to_float(value: Any, default: float | None = None) -> float | None:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value: Any, default: int | None = None) -> int | None:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def check_auth(ctx: Ctx) -> None:
    expected = (config.API_TOKEN or "").strip()
    if not expected:
        return
    if ctx.token() != expected:
        raise ApiError(401, "令牌无效")


# ---------------------------------------------------------------------------
# 系统信息
# ---------------------------------------------------------------------------
@route("GET", r"/api/health", summary="健康检查：seekdb 版本、生效配置、语料规模、chunk 表 DDL")
def ep_health(ctx: Ctx):
    version = db.server_version()
    ok = not version.startswith("unreachable")
    stats = ingest.corpus_stats() if ok else {}
    ddl = schema.table_info() if ok else []
    emb = embedder.get_embedder()
    return 200, {
        "status": "ok" if ok else "degraded",
        "app": "溯文 SeekLit",
        "version": APP_VERSION,
        "seekdb_version": version,
        "seekdb_reachable": ok,
        "config": config.describe(),
        "embedder": emb.describe(),
        "corpus": stats,
        "chunk_table_ddl": ddl[0]["ddl"] if ddl else None,
        "server_time": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


@route("GET", r"/api/stats", auth=True, summary="语料规模与 chunk 表结构")
def ep_stats(ctx: Ctx):
    check_auth(ctx)
    ddl = schema.table_info()
    return 200, {
        "corpus": ingest.corpus_stats(),
        "chunk_table_ddl": ddl[0].get("ddl") if ddl else None,
        "seekdb_version": db.server_version(),
        "embedder": embedder.get_embedder().describe(),
    }


# ---------------------------------------------------------------------------
# 检索
# ---------------------------------------------------------------------------
@route("POST", r"/api/search", auth=True, summary="执行一次检索，返回结果 + 真实 SQL + 分数分解 + 耗时")
def ep_search(ctx: Ctx):
    check_auth(ctx)
    query_text = str(ctx.body.get("query") or "").strip()
    if not query_text:
        raise ApiError(400, "检索词不能为空")
    mode = str(ctx.body.get("mode") or "hybrid")
    fusion = str(ctx.body.get("fusion") or "rrf")
    if mode not in search.MODES:
        raise ApiError(400, f"mode 只能是 {'/'.join(search.MODES)}")
    if fusion not in search.FUSIONS:
        raise ApiError(400, f"fusion 只能是 {'/'.join(search.FUSIONS)}")
    try:
        return 200, search.search(
            query_text=query_text,
            mode=mode,
            fusion=fusion,
            semantic_weight=_to_float(ctx.body.get("semantic_weight")),
            top_k=_to_int(ctx.body.get("top_k")),
            candidate_k=_to_int(ctx.body.get("candidate_k")),
            rank_constant=_to_int(ctx.body.get("rank_constant")),
            filters=parse_filters(ctx.body.get("filters")),
            log_query=bool(ctx.body.get("log", True)),
        )
    except ValueError as exc:
        raise ApiError(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        log.exception("search failed")
        raise ApiError(500, f"{type(exc).__name__}: {exc}") from exc


@route("POST", r"/api/compare", auth=True, summary="三路对比：纯语义 / 纯关键词 / seekdb 原生混合，返回排名差异")
def ep_compare(ctx: Ctx):
    check_auth(ctx)
    query_text = str(ctx.body.get("query") or "").strip()
    if not query_text:
        raise ApiError(400, "检索词不能为空")
    try:
        return 200, search.compare_modes(
            query_text, parse_filters(ctx.body.get("filters")), _to_int(ctx.body.get("top_k")))
    except Exception as exc:  # noqa: BLE001
        log.exception("compare failed")
        raise ApiError(500, f"{type(exc).__name__}: {exc}") from exc


@route("GET", r"/api/chunks/(?P<chunk_id>\d+)", auth=True, summary="回表取一个分块的完整原文与出处元数据")
def ep_chunk(ctx: Ctx, chunk_id: str):
    check_auth(ctx)
    row = db.query_one(
        "SELECT chunk_id,doc_id,doc_title,doc_kind,source_path,lang,page_no,section_path,"
        "line_start,line_end,chunk_index,tags,content,content_hash,created_at "
        "FROM `chunk` WHERE chunk_id=%s LIMIT 1", (int(chunk_id),))
    if not row:
        raise ApiError(404, "分块不存在")
    return 200, row


@route("GET", r"/api/chunks/(?P<chunk_id>\d+)/related", auth=True,
       summary="以段找段：用该分块已存储的向量在 VECTOR 索引上做近邻检索")
def ep_related(ctx: Ctx, chunk_id: str):
    check_auth(ctx)
    k = ctx.q_int("k", 6) or 6
    rows, sql = search.related_chunks(int(chunk_id), k)
    return 200, {"chunk_id": int(chunk_id), "sql": [sql] if sql else [], "hits": rows}


@route("GET", r"/api/logs", auth=True, summary="读取 search_log 表里最近的检索记录")
def ep_logs(ctx: Ctx):
    check_auth(ctx)
    return 200, {"logs": search.recent_logs(ctx.q_int("limit", 30) or 30)}


@route("GET", r"/api/preset-queries", summary="语料里挑出的真实问题，供演示与效果验证复用")
def ep_presets(ctx: Ctx):
    return 200, {"queries": list_preset_queries()}


# ---------------------------------------------------------------------------
# 文档库与导入
# ---------------------------------------------------------------------------
@route("GET", r"/api/documents", auth=True, summary="列出 doc 表里的全部文档")
def ep_documents(ctx: Ctx):
    check_auth(ctx)
    return 200, {"documents": ingest.list_docs(), "stats": ingest.corpus_stats()}


@route("DELETE", r"/api/documents/(?P<doc_id>[0-9a-zA-Z_-]+)", auth=True,
       summary="从 seekdb 删除一篇文档及其所有分块")
def ep_delete_document(ctx: Ctx, doc_id: str):
    check_auth(ctx)
    n = ingest.delete_doc(unquote(doc_id))
    return 200, {"deleted_chunks": n, "doc_id": doc_id}


@route("GET", r"/api/ingest/preview", auth=True, summary="扫描语料目录，标注哪些文件已导入")
def ep_ingest_preview(ctx: Ctx):
    check_auth(ctx)
    entries = ingest.scan_corpus()
    known = ingest.existing_hashes()
    return 200, {
        "corpus_dir": str(config.CORPUS_DIR),
        "files": [{
            "rel": e.rel, "kind": e.doc_kind, "size": e.size,
            "sha256": e.sha256[:16], "tags": e.tags,
            "already_imported": e.sha256 in known,
        } for e in entries],
    }


@route("POST", r"/api/ingest", auth=True, summary="导入语料：mode=rebuild 全量重建 / incremental 增量")
def ep_ingest(ctx: Ctx):
    check_auth(ctx)
    mode = str(ctx.body.get("mode") or "rebuild")
    if mode not in ("rebuild", "incremental"):
        raise ApiError(400, "mode 只能是 rebuild 或 incremental")
    try:
        report = ingest.run_import(mode=mode, limit=_to_int(ctx.body.get("limit")))
    except Exception as exc:  # noqa: BLE001
        log.exception("ingest failed")
        raise ApiError(500, f"{type(exc).__name__}: {exc}") from exc
    return 200, report.as_dict()


# ---------------------------------------------------------------------------
# 快照导出（供 Qoder Sites 公开站点做离线交互演示）
# ---------------------------------------------------------------------------
@route("POST", r"/api/snapshot/export", auth=True,
       summary="导出检索结果快照 JSON，供公开站点离线演示（分数全部来自真实 seekdb 查询）")
def ep_snapshot_export(ctx: Ctx):
    check_auth(ctx)
    t0 = time.perf_counter()
    weights = ctx.body.get("weights")
    if not isinstance(weights, list) or not weights:
        weights = [0.0, 0.3, 0.5, 0.7, 1.0]
    snap = build_snapshot(
        queries=_as_str_list(ctx.body.get("queries")),
        top_k=_to_int(ctx.body.get("top_k"), 10) or 10,
        candidate_k=_to_int(ctx.body.get("candidate_k"), 40) or 40,
        weights=[float(w) for w in weights],
    )
    snap["build_seconds"] = round(time.perf_counter() - t0, 2)
    config.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    out = config.SNAPSHOT_DIR / "seeklit_snapshot.json"
    out.write_text(json.dumps(snap, ensure_ascii=False, indent=1), encoding="utf-8")
    return 200, {
        "path": str(out), "queries": len(snap.get("queries", [])),
        "size_bytes": out.stat().st_size, "build_seconds": snap["build_seconds"],
    }


# ---------------------------------------------------------------------------
# 接口清单（替代 Swagger，零依赖）
# ---------------------------------------------------------------------------
@route("GET", r"/api/docs", summary="本服务的接口清单")
def ep_docs(ctx: Ctx):
    return 200, {
        "app": "溯文 SeekLit",
        "version": APP_VERSION,
        "runtime": f"python {sys.version.split()[0]} · http.server (stdlib, 0 web deps)",
        "database": config.describe()["seekdb"],
        "endpoints": [
            {"method": r.method, "path": r.regex.pattern.strip("^$").replace(r"(?P<chunk_id>\d+)", "{chunk_id}")
                                 .replace(r"(?P<doc_id>[0-9a-zA-Z_-]+)", "{doc_id}"),
             "auth": r.auth, "summary": r.summary}
            for r in _ROUTES
        ],
    }


# ---------------------------------------------------------------------------
# HTTP Handler
# ---------------------------------------------------------------------------
CORS_ORIGINS = [o.strip() for o in config.CORS_ORIGINS.split(",") if o.strip()]

# 常见静态类型的兜底（部分精简系统的 mimetypes 库不全）
_EXTRA_MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".woff2": "font/woff2",
    ".txt": "text/plain; charset=utf-8",
    ".map": "application/json; charset=utf-8",
}


def _mime_for(path: Path) -> str:
    guess = mimetypes.guess_type(path.name)[0]
    return _EXTRA_MIME.get(path.suffix.lower(), guess or "application/octet-stream")


class Handler(BaseHTTPRequestHandler):
    server_version = f"SeekLit/{APP_VERSION}"
    protocol_version = "HTTP/1.1"

    # -- 基础输出 ---------------------------------------------------------
    def _cors(self) -> None:
        origin = self.headers.get("Origin") or ""
        if not CORS_ORIGINS or "*" in CORS_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin or "*")
        elif origin in CORS_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-SeekLit-Token")
        self.send_header("Access-Control-Max-Age", "600")

    def _send_bytes(self, status: int, body: bytes, ctype: str,
                    extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8")

    def _send_file(self, path: Path, cache: str = "no-cache") -> None:
        try:
            data = path.read_bytes()
        except OSError:
            self._send_json(404, {"detail": f"文件不存在: {path.name}"})
            return
        self._send_bytes(200, data, _mime_for(path), {"Cache-Control": cache})

    # -- 请求分发 ---------------------------------------------------------
    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch("PUT")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    def _read_body(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > 8 * 1024 * 1024:
            raise ApiError(413, "请求体过大（上限 8 MB）")
        raw = self.rfile.read(length)
        if not raw.strip():
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError(400, f"请求体不是合法 JSON：{exc}") from exc

    def _dispatch(self, method: str) -> None:
        started = time.perf_counter()
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query, keep_blank_values=True)

        try:
            if method in ("GET", "HEAD") and not path.startswith("/api/"):
                self._serve_static(path)
                return

            body = self._read_body() if method in ("POST", "PUT", "DELETE") else {}
            ctx = Ctx(query, body, self.headers)

            for r in _ROUTES:
                if r.method != method:
                    continue
                m = r.regex.match(path)
                if not m:
                    continue
                status, payload = r.handler(ctx, **m.groupdict())
                self._send_json(status, payload)
                self._access_log(method, path, status, started)
                return

            self._send_json(404, {"detail": f"没有这个接口: {method} {path}",
                                  "hint": "接口清单见 GET /api/docs"})
            self._access_log(method, path, 404, started)
        except ApiError as exc:
            self._send_json(exc.status, {"detail": exc.detail})
            self._access_log(method, path, exc.status, started)
        except BrokenPipeError:
            pass  # 客户端提前断开，无需回应
        except Exception as exc:  # noqa: BLE001
            log.error("未处理异常 %s %s\n%s", method, path, traceback.format_exc())
            try:
                self._send_json(500, {"detail": f"{type(exc).__name__}: {exc}"})
            except Exception:  # noqa: BLE001
                pass
            self._access_log(method, path, 500, started)

    def _serve_static(self, path: str) -> None:
        if path in ("/", "/index.html"):
            self._send_file(STATIC_DIR / "index.html")
            return
        if path == "/favicon.ico":
            icon = STATIC_DIR / "favicon.svg"
            if icon.exists():
                self._send_file(icon, "public, max-age=86400")
            else:
                self._send_bytes(204, b"", "image/x-icon")
            return
        # 只允许 static 目录内的文件，并阻止 ../ 越界
        rel = path.lstrip("/")
        target = (STATIC_DIR / rel).resolve()
        try:
            target.relative_to(STATIC_DIR.resolve())
        except ValueError:
            self._send_json(403, {"detail": "禁止访问该路径"})
            return
        if target.is_file():
            self._send_file(target, "public, max-age=300")
            return
        # SPA 兜底：未知前端路径回到 index.html
        if not rel or "." not in os.path.basename(rel):
            self._send_file(STATIC_DIR / "index.html")
            return
        self._send_json(404, {"detail": f"文件不存在: {rel}"})

    def _access_log(self, method: str, path: str, status: int, started: float) -> None:
        ms = (time.perf_counter() - started) * 1000
        log.info("%s %s -> %d (%.1f ms)", method, path, status, ms)

    def log_message(self, fmt: str, *args) -> None:  # 关掉默认 stderr 噪声
        return


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------
def serve(host: str | None = None, port: int | None = None) -> None:
    host = host or config.API_HOST
    port = int(port or config.API_PORT)
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    log.info("溯文 SeekLit 已启动： http://%s:%d  （接口清单 /api/docs）",
             "127.0.0.1" if host in ("0.0.0.0", "::") else host, port)
    log.info("seekdb: %s:%s/%s", config.SEEKDB_HOST, config.SEEKDB_PORT, config.SEEKDB_DATABASE)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("收到中断，正在关闭…")
    finally:
        threading.Thread(target=httpd.shutdown, daemon=True).start()
        httpd.server_close()


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="溯文 SeekLit HTTP 服务")
    parser.add_argument("--host", default=None, help=f"监听地址，默认 {config.API_HOST}")
    parser.add_argument("--port", type=int, default=None, help=f"监听端口，默认 {config.API_PORT}")
    args = parser.parse_args()
    serve(args.host, args.port)


if __name__ == "__main__":
    main()

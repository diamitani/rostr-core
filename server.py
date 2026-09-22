def _request_path(handler: BaseHTTPRequestHandler) -> str:
    parsed = urlparse(handler.path)
    path = parsed.path.rstrip("/") or "/"
    qs = parse_qs(parsed.query)
    if qs.get("__path"):
        return qs["__path"][0].rstrip("/") or "/"
    if path.startswith("/api/"):
        path = "/" + path[5:]
        path = path.rstrip("/") or "/"
    sid = (qs.get("id") or [""])[0]
    mapping = {
        "/": "/v1/health",
        "/health": "/v1/health",
        "/index": "/v1/health",
        "/index.py": "/v1/health",
        "/sessions": "/v1/sessions",
        "/generate": "/v1/generate",
        "/session": f"/v1/sessions/{sid}",
        "/turns": f"/v1/sessions/{sid}/turns",
    }
    if path in mapping:
        return mapping[path]
    if path.startswith("/v1"):
        return path
    return path

"""Tool registry with allow/deny enforcement.

Tools are plain Python functions. The registry checks every call against the
policy in config.json BEFORE running anything. Composio plugs in here —
see the extension point at the bottom.
"""
import os


class ToolError(RuntimeError):
    pass


class ToolRegistry:
    def __init__(self, allow, deny):
        self.allow = set(allow or [])
        self.deny = set(deny or [])
        self._tools = {}

    def register(self, name, fn, description="", schema=None):
        self._tools[name] = {
            "fn": fn, "description": description, "schema": schema or {},
        }

    def describe(self):
        return {n: {"description": t["description"], "schema": t["schema"]}
                for n, t in self._tools.items()}

    def call(self, name, args):
        if name in self.deny:
            return {"ok": False, "error": f"tool '{name}' is denied by policy"}
        if name not in self.allow:
            return {"ok": False, "error": f"tool '{name}' is not in the allow-list"}
        tool = self._tools.get(name)
        if not tool:
            return {"ok": False, "error": f"unknown tool '{name}'"}
        try:
            return {"ok": True, "result": tool["fn"](**(args or {}))}
        except Exception as e:  # tools must never crash the loop
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _sandbox(path):
    """Keep file tools inside the current working directory."""
    root = os.path.realpath(os.getcwd())
    p = os.path.realpath(os.path.join(root, path))
    if not (p == root or p.startswith(root + os.sep)):
        raise ToolError(f"path escapes sandbox: {path}")
    return p


def read_file(path):
    with open(_sandbox(path), "r", encoding="utf-8") as f:
        return f.read()


def write_file(path, content):
    p = _sandbox(path)
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(content)
    return f"wrote {len(content)} chars to {path}"


def list_dir(path="."):
    return sorted(os.listdir(_sandbox(path)))


def default_registry(config):
    """The built-in toolset. Small on purpose — add yours here."""
    r = ToolRegistry(config["tools"]["allow"], config["tools"]["deny"])
    r.register("read_file", read_file,
               "Read a text file inside the working directory.",
               {"path": "str — relative path"})
    r.register("write_file", write_file,
               "Write text to a file inside the working directory.",
               {"path": "str", "content": "str"})
    r.register("list_dir", list_dir,
               "List files in a directory inside the working directory.",
               {"path": "str — default '.'"})
    return r


# ---------------------------------------------------------------------------
# EXTENSION POINT: Composio
# Instead of hand-writing 50 integrations, attach Composio here:
#
#   from composio import ComposioToolSet
#   def attach_composio(registry, api_key, entity_id="default"):
#       toolset = ComposioToolSet(api_key=api_key)
#       for t in toolset.get_tools(entity_id=entity_id):   # e.g. gmail_send, ...
#           registry.register(t.name, t.execute, t.description, t.schema)
#       registry.allow |= {t.name for t in ...}  # then gate via config
#
# The runtime doesn't care where tools came from — it just calls registry.
# ---------------------------------------------------------------------------

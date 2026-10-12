"""检查已跟踪 Python 源码中的硬编码凭据，不输出凭据内容。"""
import ast
from pathlib import Path
import re
import subprocess
import sys

SENSITIVE = re.compile(r"(?:password|secret|api_key|access_token)$", re.I)


def literal_credentials(source):
    tree = ast.parse(source)
    findings = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        value = node.value
        if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            name = target.id if isinstance(target, ast.Name) else getattr(target, "attr", "")
            if SENSITIVE.search(name) and value.value and not value.value.startswith(("${", "<")):
                findings.append(node.lineno)
    return findings


def main():
    files = subprocess.check_output(["git", "ls-files", "-z"]).decode("utf-8").split("\0")
    bad = []
    for name in files:
        path = Path(name)
        if name == ".env":
            bad.append(".env must not be tracked")
        elif path.suffix == ".py" and not name.startswith("tests/"):
            for line in literal_credentials(path.read_text(encoding="utf-8-sig")):
                bad.append(f"{name}:{line}: literal credential")
    for finding in bad:
        print(finding)
    print(f"Credential source scan: {len(bad)} findings")
    return bool(bad)


if __name__ == "__main__":
    sys.exit(main())

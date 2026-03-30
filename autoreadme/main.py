import argparse
import ast
import json
import re
import sys
import unicodedata
import yaml
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress
from rich.table import Table

console = Console()

# Polyfill for stdlib detection (Python < 3.10 fallback)
STDLIB_MODULES = sys.stdlib_module_names if hasattr(sys, 'stdlib_module_names') else {'os', 'sys', 'math', 'json', 'ast', 're', 'pathlib', 'itertools', 'collections', 'typing'}

DEFAULT_CONFIG = {
    "ignore_dirs": [".git", "venv", "__pycache__", "env", ".tox", "build", "dist", "node_modules"],
    "ignore_files": ["setup.py", "__init__.py"],
    "include_private": False,
    "max_complexity": 10,  # Threshold for warning
}

def load_config(target_dir: Path) -> dict:
    config = DEFAULT_CONFIG.copy()
    config_path = target_dir / ".autoreadme.yml"
    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config.update(yaml.safe_load(f) or {})
        except Exception:
            pass
    return config

def slugify(value: str) -> str:
    value = unicodedata.normalize('NFKD', value).encode('ascii', 'ignore').decode('ascii')
    value = re.sub(r'[^\w\s-]', '', value.lower())
    return re.sub(r'[-\s]+', '-', value).strip('-')

def get_full_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    try:
        arg_str = ast.unparse(node.args).replace(', ', ',')  # tighter formatting
        return_str = f" -> {ast.unparse(node.returns)}" if node.returns else ""
        return f"({arg_str}){return_str}"
    except Exception:
        return "(...)"

def calculate_complexity(node: ast.AST) -> int:
    """Calculates Cyclomatic Complexity (McCabe)."""
    complexity = 1
    for child in ast.walk(node):
        if isinstance(child, (ast.If, ast.While, ast.For, ast.ExceptHandler, ast.With)):
            complexity += 1
        elif hasattr(ast, 'Match') and isinstance(child, getattr(ast, 'Match')):
            complexity += len(child.cases)
        elif isinstance(child, ast.BoolOp):
            complexity += len(child.values) - 1
    return complexity

def lint_docstring(node: ast.FunctionDef | ast.AsyncFunctionDef, doc: str) -> list[str]:
    """Checks if parameters and return types are mentioned in the docstring."""
    issues = []
    if not doc:
        return ["⚠️ Missing entirely"]
    
    doc_lower = doc.lower()
    for arg in node.args.args:
        if arg.arg not in ['self', 'cls'] and arg.arg not in doc_lower:
            issues.append(f"⚠️ Param '{arg.arg}' undocumented")
            
    if node.returns and "return" not in doc_lower and "yield" not in doc_lower:
        issues.append("⚠️ Return type undocumented")
        
    return issues

class ProjectAnalyzer(ast.NodeVisitor):
    def __init__(self, include_private: bool):
        self.include_private = include_private
        self.data = {
            "module_doc": "",
            "classes": {},
            "functions": {},
            "raw_imports": set(), # Processed later into stdlib/external/internal
            "metrics": {"documented": 0, "total": 0, "complexity_warnings": 0}
        }
        self.context_stack = []

    def _should_ignore(self, name: str) -> bool:
        if self.include_private: return False
        return name.startswith('_') and not name.startswith('__')

    def _update_metrics(self, has_doc: bool):
        self.data["metrics"]["total"] += 1
        if has_doc:
            self.data["metrics"]["documented"] += 1

    def visit_Module(self, node: ast.Module):
        doc = ast.get_docstring(node)
        self.data["module_doc"] = doc or ""
        self._update_metrics(bool(doc))
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import):
        for alias in node.names:
            self.data["raw_imports"].add(alias.name.split('.')[0])

    def visit_ImportFrom(self, node: ast.ImportFrom):
        if node.module:
            # Handle relative imports level
            prefix = "." * node.level if node.level > 0 else ""
            self.data["raw_imports"].add(f"{prefix}{node.module}")

    def visit_ClassDef(self, node: ast.ClassDef):
        if self._should_ignore(node.name): return
        full_name = ".".join(self.context_stack + [node.name])
        doc = ast.get_docstring(node)
        self._update_metrics(bool(doc))

        self.data["classes"][full_name] = {
            "doc": doc or "No description provided.",
            "bases": f"({', '.join([ast.unparse(b) for b in node.bases])})" if node.bases else "",
            "methods": {}
        }

        self.context_stack.append(node.name)
        self.generic_visit(node)
        self.context_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef):
        if not self._should_ignore(node.name):
            self._process_callable(node, is_async=False)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
        if not self._should_ignore(node.name):
            self._process_callable(node, is_async=True)

    def _process_callable(self, node, is_async: bool):
        doc = ast.get_docstring(node)
        self._update_metrics(bool(doc))
        complexity = calculate_complexity(node)
        lint_issues = lint_docstring(node, doc)
        
        info = {
            "sig": f"{'async ' if is_async else ''}{node.name}{get_full_signature(node)}",
            "doc": doc or "No docstring provided.",
            "complexity": complexity,
            "lint_issues": lint_issues,
            "type": "Method" if self.context_stack else "Function"
        }

        if self.context_stack:
            parent = ".".join(self.context_stack)
            if parent in self.data["classes"]:
                self.data["classes"][parent]["methods"][node.name] = info
        else:
            self.data["functions"][node.name] = info

def process_single_file(file_path: Path, root_path: Path, include_private: bool) -> tuple:
    rel_path = str(file_path.relative_to(root_path))
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read())
            visitor = ProjectAnalyzer(include_private=include_private)
            visitor.visit(tree)
            return rel_path, visitor.data, None
    except Exception as e:
        return rel_path, None, str(e)

def resolve_dependencies(project_data: dict):
    """Categorizes raw imports into Stdlib, External, and Internal Dependencies."""
    files = project_data["files"]
    internal_modules = {str(Path(f).with_suffix('')).replace('/', '.').replace('\\', '.') for f in files.keys()}
    internal_modules.update({Path(f).stem for f in files.keys()})

    for f_name, data in files.items():
        data["deps"] = {"stdlib": [], "external": [], "internal": []}
        for imp in data.get("raw_imports", []):
            base_mod = imp.lstrip('.').split('.')[0]
            if base_mod in STDLIB_MODULES:
                data["deps"]["stdlib"].append(imp)
            elif imp in internal_modules or base_mod in internal_modules:
                data["deps"]["internal"].append(imp)
            else:
                data["deps"]["external"].append(imp)
        del data["raw_imports"]

def generate_markdown(project_data: dict, output_file: str, max_complex: int):
    files = project_data["files"]
    total_doc = sum(f["metrics"]["documented"] for f in files.values() if f)
    total_items = sum(f["metrics"]["total"] for f in files.values() if f)
    coverage = int((total_doc / total_items * 100)) if total_items > 0 else 0
    color = "green" if coverage > 80 else "yellow" if coverage > 50 else "red"

    md = [
        "# Project Documentation\n",
        f"![Coverage](https://img.shields.io/badge/Coverage-{coverage}%25-{color})\n",
        "> *Generated by Auto-README Elite Analyzer*\n\n",
        "## 📚 Table of Contents\n"
    ]

    for f_name in files:
        md.append(f"- [📁 `{f_name}`](#{slugify('file-' + f_name)})")
    md.append("\n---\n")

    for f_name, data in files.items():
        anchor = slugify(f"file-{f_name}")
        md.append(f"## 📁 `{f_name}` <a name='{anchor}'></a>\n")

        if data is None: # Parsing error
            md.append("> ❌ **Failed to parse this file.**\n\n---\n")
            continue

        if data["module_doc"]:
            md.append(f"*{data['module_doc']}*\n\n")

        # Dependency Badges
        deps = data["deps"]
        if deps["external"]: md.append(f"**📦 External:** `{', '.join(deps['external'])}`  ")
        if deps["internal"]: md.append(f"**🔗 Internal:** `{', '.join(deps['internal'])}`  ")
        md.append("\n")

        if data["classes"]:
            md.append("### Classes\n")
            for c_name, c_info in data["classes"].items():
                md.append(f"<details><summary><b>class {c_name}</b>{c_info['bases']}</summary>\n\n")
                md.append(f"> {c_info['doc'].splitlines()[0]}\n\n")
                
                if c_info["methods"]:
                    md.append("| Method | Complexity | Lint | Description |")
                    md.append("| :--- | :---: | :--- | :--- |")
                    for m_name, m_info in c_info["methods"].items():
                        cc = m_info["complexity"]
                        cc_str = f"🔴 {cc}" if cc > max_complex else f"🟢 {cc}"
                        lint = "<br>".join(m_info["lint_issues"]) or "✅ Pass"
                        clean_doc = m_info["doc"].splitlines()[0]
                        md.append(f"| `{m_name}` | {cc_str} | {lint} | {clean_doc} |")
                    md.append("\n")
                md.append("</details>\n")

        if data["functions"]:
            md.append("### Global Functions\n")
            for f_name, f_info in data["functions"].items():
                cc = f_info["complexity"]
                cc_str = f"🔴 {cc}" if cc > max_complex else f"🟢 {cc}"
                md.append(f"<details><summary><b>{f_info['sig']}</b> (CC: {cc_str})</summary>\n")
                if f_info["lint_issues"]:
                    md.append(f"\n> **Linting:** {', '.join(f_info['lint_issues'])}")
                md.append(f"\n\n```python\n\"\"\"\n{f_info['doc']}\n\"\"\"\n```\n")
                md.append("</details>\n")

        md.append("\n---\n")

    with open(output_file, "w", encoding="utf-8") as f:
        f.write("\n".join(md))

def generate_html(project_data: dict, output_file: str):
    """Generates a standalone HTML documentation site with embedded CSS."""
    # A simple but highly effective HTML template
    html = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Project Documentation</title>
        <style>
            body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; line-height: 1.6; color: #333; max-width: 1000px; margin: 0 auto; padding: 2rem; background: #f9fafb; }
            h1, h2, h3 { color: #111827; }
            .file-card { background: white; border-radius: 8px; padding: 1.5rem; margin-bottom: 2rem; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
            .badge { display: inline-block; padding: 0.25em 0.5em; font-size: 0.75em; font-weight: 700; border-radius: 4px; margin-right: 0.5rem; }
            .badge-ext { background: #dbeafe; color: #1e3a8a; }
            .badge-int { background: #dcfce7; color: #166534; }
            .badge-warn { background: #fee2e2; color: #991b1b; }
            details { border: 1px solid #e5e7eb; border-radius: 4px; padding: 0.5rem 1rem; margin-bottom: 0.5rem; background: #fafafa; }
            summary { font-weight: 600; cursor: pointer; outline: none; }
            code { background: #f1f5f9; padding: 0.2rem 0.4rem; border-radius: 3px; font-family: monospace; font-size: 0.9em;}
            table { width: 100%; border-collapse: collapse; margin-top: 1rem; }
            th, td { text-align: left; padding: 8px; border-bottom: 1px solid #e5e7eb; }
        </style>
    </head>
    <body>
        <h1>📚 Project Documentation</h1>
        <p>Generated by Auto-README Elite.</p>
    """
    
    for f_name, data in project_data["files"].items():
        if not data: continue
        html += f"<div class='file-card'><h2 id='{slugify(f_name)}'>📁 {f_name}</h2>"
        if data["module_doc"]: html += f"<p><em>{data['module_doc']}</em></p>"
        
        if data["deps"]["external"]:
            html += f"<span class='badge badge-ext'>📦 External: {', '.join(data['deps']['external'])}</span>"
        if data["deps"]["internal"]:
            html += f"<span class='badge badge-int'>🔗 Internal: {', '.join(data['deps']['internal'])}</span>"
        
        if data["classes"]:
            html += "<h3>Classes</h3>"
            for c_name, c_info in data["classes"].items():
                html += f"<details><summary>{c_name}{c_info['bases']}</summary>"
                html += f"<p>{c_info['doc']}</p><table><tr><th>Method</th><th>Complexity</th><th>Issues</th></tr>"
                for m_name, m_info in c_info["methods"].items():
                    lint = "<br>".join(m_info["lint_issues"]) or "✅ Pass"
                    html += f"<tr><td><code>{m_name}</code></td><td>{m_info['complexity']}</td><td>{lint}</td></tr>"
                html += "</table></details>"
                
        html += "</div>"
    
    html += "</body></html>"
    
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(html)

def main():
    parser = argparse.ArgumentParser(description="Elite AST-based Project Analyzer")
    parser.add_argument("dir", help="Directory to analyze")
    parser.add_argument("-o", "--output", help="Output file name")
    parser.add_argument("-f", "--format", choices=["md", "json", "html"], default="md")
    args = parser.parse_args()

    root = Path(args.dir)
    if not root.is_dir():
        console.print(f"[bold red]Error:[/bold red] Invalid directory.")
        return

    config = load_config(root)
    py_files = [f for f in root.rglob("*.py") if not any(i in f.parts for i in config["ignore_dirs"]) and f.name not in config["ignore_files"]]

    project_data = {"files": {}}
    errors = 0

    with Progress() as progress:
        task = progress.add_task("[cyan]Analyzing Code Metrics...", total=len(py_files))
        with ThreadPoolExecutor() as executor:
            futures = {executor.submit(process_single_file, f, root, config["include_private"]): f for f in py_files}
            for future in as_completed(futures):
                rel_path, data, err = future.result()
                if err:
                    errors += 1
                    project_data["files"][rel_path] = None # Store error state
                else:
                    project_data["files"][rel_path] = data
                progress.update(task, advance=1)

    resolve_dependencies(project_data)

    # Output Routing
    if args.format == "json":
        out_file = args.output or "docs.json"
        with open(out_file, 'w', encoding='utf-8') as f: json.dump(project_data, f, indent=2)
    elif args.format == "html":
        out_file = args.output or "docs.html"
        generate_html(project_data, out_file)
    else:
        out_file = args.output or "README.md"
        generate_markdown(project_data, out_file, config["max_complexity"])
    
    # Final Table Output
    summary = Table(title="Analysis Complete", show_header=True, header_style="bold magenta")
    summary.add_column("Metric"); summary.add_column("Value")
    summary.add_row("Files Processed", str(len(project_data["files"])))
    summary.add_row("Format", args.format.upper())
    summary.add_row("Output", out_file)
    
    console.print(Panel(summary, border_style="green", expand=False))

if __name__ == "__main__":
    main()
import argparse
import ast
import re
from pathlib import Path
from rich.console import Console
from rich.panel import Panel

console = Console()

def get_type_hint(node):
    """Helper function to cleanly extract type hints from AST nodes."""
    if node is None:
        return ""
    try:
        return ast.unparse(node)
    except AttributeError:
        if isinstance(node, ast.Name):
            return node.id
        return ""

def parse_requirements(directory_path):
    """Looks for a requirements.txt file and extracts the package names."""
    req_path = Path(directory_path) / "requirements.txt"
    dependencies = []
    
    if req_path.exists():
        with open(req_path, 'r', encoding='utf-8') as f:
            for line in f:
                clean_line = line.strip()
                if clean_line and not clean_line.startswith('#'):
                    package_name = re.split(r'[=<>~]', clean_line)[0].strip()
                    if package_name:
                        dependencies.append(package_name)
    return dependencies

# --- NEW: The Offline Profiler ---
def guess_script_purpose(imports):
    """Analyzes imports to guess what the script does."""
    categories = {
        "🖥️ Command Line (CLI) Tool": ['argparse', 'click', 'typer', 'sys'],
        "🌐 Web Requests & Networking": ['requests', 'urllib', 'aiohttp', 'socket', 'http'],
        "🕸️ Web Application": ['flask', 'django', 'fastapi', 'starlette'],
        "📊 Data Science & Math": ['pandas', 'numpy', 'math', 'statistics', 'scipy'],
        "🕷️ Web Scraping": ['bs4', 'selenium', 'playwright', 'scrapy'],
        "📁 File System Operations": ['os', 'pathlib', 'shutil', 'csv', 'json', 'zipfile'],
        "🎨 UI & Terminal Formatting": ['rich', 'curses', 'tkinter', 'PyQt5', 'colorama'],
        "🤖 Machine Learning": ['torch', 'tensorflow', 'sklearn', 'keras'],
        "🧪 Automated Testing": ['unittest', 'pytest', 'mock']
    }
    
    detected = set()
    for imp in imports:
        for category, libs in categories.items():
            if imp in libs:
                detected.add(category)
                
    if detected:
        return f"Based on imports, this script is focused on: **{', '.join(detected)}**."
    return "General Utility Script (No specific domain detected)."

def parse_python_file(file_path):
    """Reads a Python file, extracts docstrings, and profiles the script."""
    with open(file_path, 'r', encoding='utf-8') as f:
        try:
            tree = ast.parse(f.read())
            
            mod_doc = ast.get_docstring(tree)
            if not mod_doc:
                mod_doc = "⚠️ *No module description provided.*"
                
            file_data = {"module_doc": mod_doc, "classes": {}, "functions": {}, "purpose": ""}
            
            # --- NEW: Extracting Imports ---
            imports = set()

            for node in tree.body:
                # Catch "import X"
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        imports.add(alias.name.split('.')[0])
                # Catch "from X import Y"
                elif isinstance(node, ast.ImportFrom):
                    if node.module:
                        imports.add(node.module.split('.')[0])
                        
                elif isinstance(node, ast.ClassDef):
                    doc = ast.get_docstring(node)
                    file_data["classes"][node.name] = doc if doc else "⚠️ *No class docstring provided.*"
                    
                elif isinstance(node, ast.FunctionDef):
                    doc = ast.get_docstring(node)
                    
                    args_list = []
                    for arg in node.args.args:
                        if arg.arg != 'self':
                            arg_str = arg.arg
                            if arg.annotation:
                                hint = get_type_hint(arg.annotation)
                                if hint:
                                    arg_str += f": {hint}"
                            args_list.append(arg_str)
                            
                    arg_string = ", ".join(args_list)
                    
                    return_type = ""
                    if node.returns:
                        hint = get_type_hint(node.returns)
                        if hint:
                            return_type = f" -> {hint}"

                    if not doc:
                        if arg_string or return_type:
                            doc = f"⚠️ *No docstring.* Signature: `({arg_string}){return_type}`"
                        else:
                            doc = "⚠️ *No docstring provided.*"
                            
                    file_data["functions"][node.name] = doc

            # Pass the gathered imports to our profiler
            file_data["purpose"] = guess_script_purpose(imports)

            return file_data
            
        except Exception as e:
            console.print(f"[bold red]❌ Error parsing {file_path.name}:[/bold red] {e}")
            return None

def scan_directory(directory_path):
    """Scans the directory with a sleek terminal spinner."""
    path = Path(directory_path)
    
    if not path.exists() or not path.is_dir():
        console.print(f"[bold red]❌ Error:[/bold red] '{directory_path}' is not a valid directory.")
        return None

    project_metadata = {
        "dependencies": parse_requirements(directory_path),
        "files": {}
    }

    with console.status(f"[bold cyan]Scanning '{directory_path}' for Python files...", spinner="dots"):
        for py_file in path.rglob('*.py'):
            if any(ignore in py_file.parts for ignore in ['venv', '__pycache__', '.git']):
                continue
                
            file_data = parse_python_file(py_file)
            
            if file_data:
                project_metadata["files"][py_file.name] = file_data
                console.print(f"[green]✓[/green] Analyzed: [bold]{py_file.name}[/bold]")
                
    return project_metadata

def generate_markdown(project_metadata, output_file="README.md"):
    """Formats data into Markdown, injects project stats, and outputs a success panel."""
    if not project_metadata["files"]:
        console.print("[bold yellow]⚠️ No Python files found to document.[/bold yellow]")
        return
        
    total_files = len(project_metadata["files"])
    total_classes = sum(len(data["classes"]) for data in project_metadata["files"].values())
    total_funcs = sum(len(data["functions"]) for data in project_metadata["files"].values())
    
    md_lines = [
        "# Project Documentation\n",
        "> *Autogenerated by Auto-README Generator.*\n\n",
        "## 📊 Project Overview\n",
        f"- **Total Python Files:** {total_files}\n",
        f"- **Total Classes:** {total_classes}\n",
        f"- **Total Functions:** {total_funcs}\n\n",
        "---\n"
    ]
    
    deps = project_metadata.get("dependencies", [])
    if deps:
        md_lines.append("## ⚙️ Installation & Requirements\n")
        md_lines.append("To run this project, you will need to install the following dependencies:\n")
        md_lines.append("```bash\npip install -r requirements.txt\n```\n")
        md_lines.append("**Packages used:**\n")
        for dep in deps:
            md_lines.append(f"- `{dep}`")
        md_lines.append("\n---\n")

    for file_name, data in project_metadata["files"].items():
        md_lines.append(f"## 📁 File: `{file_name}`\n")
        
        # --- NEW: Inject the guessed purpose right at the top of the file section ---
        if data["purpose"]:
            md_lines.append(f"> 💡 **Script Profile:** {data['purpose']}\n\n")
            
        if data["module_doc"]:
            md_lines.append(f"{data['module_doc'].strip()}\n\n")
            
        if data["classes"]:
            md_lines.append("### Classes\n")
            for cls_name, cls_doc in data["classes"].items():
                md_lines.append(f"- **`{cls_name}`**")
                md_lines.append(f"  - {cls_doc.strip().split(chr(10))[0]}")
            md_lines.append("\n")
                
        if data["functions"]:
            md_lines.append("### Functions\n")
            for func_name, func_doc in data["functions"].items():
                md_lines.append(f"- **`{func_name}()`**")
                md_lines.append(f"  - {func_doc.strip().split(chr(10))[0]}")
            md_lines.append("\n")
            
        md_lines.append("---\n")
        
    with open(output_file, 'w', encoding='utf-8') as f:
        f.writelines(line + "\n" for line in md_lines)
        
    success_msg = f"Successfully generated [bold cyan]{output_file}[/bold cyan]!\nCheck your directory to view the markdown."
    console.print() 
    console.print(Panel(success_msg, title="[bold green]✅ Scan Complete", border_style="green", expand=False))

def main():
    """The main entry point for the CLI."""
    parser = argparse.ArgumentParser(description="Auto-README Generator: Scans a project and extracts docstrings.")
    parser.add_argument("target_dir", help="The path to the Python project directory you want to scan.")
    
    args = parser.parse_args()
    
    project_metadata = scan_directory(args.target_dir)
    
    if project_metadata:
        generate_markdown(project_metadata)

if __name__ == "__main__":
    main()
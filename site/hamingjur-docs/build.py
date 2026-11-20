#!/usr/bin/env python3
"""Build the standalone guide from the checkout's public Python declarations.

Run from any directory with Python 3.9+. No imports of Hamingjur, optional
dependencies, external assets, or network access are needed.
"""
from __future__ import annotations

import argparse
import ast
import html
import json
import re
from pathlib import Path


SOURCE_URL = 'https://github.com/adamdeprince/hamingjur/blob/master/'
ORDER = ('terminal', 'graphics', 'imaging', 'silkie', 'protocols', 'kitty',
         'ui.desktop', 'ui.widgets', 'ui.layout', 'ui.dialogs', 'ui.editor',
         'keyboard', 'mouse', 'line_editor', 'window', 'remote_control',
         'control', 'shell', 'color', 'colorspace', 'cursor_shape', 'dec_modes',
         'formatters', 'sequences', 'win_terminal')


def esc(value):
    """Escape source content for HTML text or attribute contexts."""
    return html.escape(str(value), quote=True)


def anchor(name):
    """Make a stable, unambiguous deep link from a fully qualified Python name."""
    return 'api-' + name.replace('.', '-')


def signature(node):
    """Keep positional-only, keyword-only, variadic, default and annotated arguments."""
    args = ast.unparse(node.args)
    args = re.sub(r'^(?:self|cls)(?:\s*:\s*[^,]+)?(?:,\s*|$)', '', args)
    if args.startswith('/, '):
        args = args[3:]
    result = f'({args})'
    if node.returns:
        result += ' -> ' + ast.unparse(node.returns)
    return result


def members(node):
    """Describe public class fields and enum values, preserving their source expressions."""
    result = []
    for child in node.body:
        if isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name):
            name = child.target.id
            value = name + ': ' + ast.unparse(child.annotation)
            if child.value:
                value += ' = ' + ast.unparse(child.value)
        elif isinstance(child, ast.Assign) and len(child.targets) == 1 and isinstance(
                child.targets[0], ast.Name):
            name = child.targets[0].id
            value = name + ' = ' + ast.unparse(child.value)
        else:
            continue
        if not name.startswith('_'):
            result.append(value)
    return result


def declaration(module, node, parent=None):
    """Create an entry without evaluating decorators, defaults, or platform imports."""
    name = f'{module}.{parent + "." if parent else ""}{node.name}'
    is_class = isinstance(node, ast.ClassDef)
    doc = ast.get_docstring(node) or ''
    field_lines = members(node) if is_class else []
    decorators = {ast.unparse(d).split('(')[0] for d in node.decorator_list}
    if is_class:
        constructor = next((n for n in node.body
                            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                            and n.name == '__init__'), None)
        display_signature = name + (signature(constructor) if constructor else '')
        if constructor and ast.get_docstring(constructor):
            doc += '\n\nConstructor\n-----------\n' + ast.get_docstring(constructor)
        if node.bases:
            display_signature += '\nBases: ' + ', '.join(ast.unparse(b) for b in node.bases)
        kind = 'class'
    elif decorators & {'property', 'cached_property', 'functools.cached_property'}:
        kind = 'property'
        display_signature = name + (' -> ' + ast.unparse(node.returns) if node.returns else '')
    else:
        kind = 'method' if parent else 'function'
        prefix = 'async ' if isinstance(node, ast.AsyncFunctionDef) else ''
        display_signature = prefix + name + signature(node)
        if decorators & {'contextlib.contextmanager', 'contextmanager'}:
            kind = 'context manager'
    summary = doc.split('\n\n')[0].replace('\n', ' ').strip() if doc else 'Public declaration.'
    return {'name': name, 'kind': kind, 'signature': display_signature, 'doc': doc,
            'summary': summary, 'fields': field_lines, 'line': node.lineno,
            'file': module.replace('.', '/') + '.py'}


def collect(package_root):
    """Collect every public module, declaration and class method in source order."""
    package = package_root / 'hamingjur'
    files = {}
    for path in package.rglob('*.py'):
        parts = path.relative_to(package).with_suffix('').parts
        if all(not part.startswith('_') for part in parts):
            files['.'.join(parts)] = path
    modules = []
    for stem in (*ORDER, *sorted(set(files) - set(ORDER))):
        path = files.get(stem)
        if path is None:
            continue
        tree = ast.parse(path.read_text(encoding='utf-8'))
        entries = []
        module = 'hamingjur.' + stem
        for node in tree.body:
            if not isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name.startswith('_'):
                continue
            entries.append(declaration(module, node))
            if isinstance(node, ast.ClassDef):
                seen = set()
                for method in node.body:
                    if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        continue
                    if method.name.startswith('_') and method.name != '__call__':
                        continue
                    if method.name in seen:
                        continue  # A property setter belongs to the same public property.
                    seen.add(method.name)
                    entries.append(declaration(module, method, node.name))
        modules.append({'name': module, 'doc': ast.get_docstring(tree) or '', 'entries': entries})
    return modules


def render_entry(entry):
    """Render native disclosure controls that also work with JavaScript disabled."""
    name = entry['name']
    source = SOURCE_URL + entry['file'] + '#L' + str(entry['line'])
    fields = ('<h5>Fields &amp; constants</h5><pre class="fields">'
              + esc('\n'.join(entry['fields'])) + '</pre>') if entry['fields'] else ''
    # Preserve entire source docstrings, including parameters, return types, errors and examples.
    doc = '<pre class="docstring">' + esc(entry['doc']) + '</pre>' if entry['doc'] else ''
    return f'''<details class="api-entry" id="{anchor(name)}" data-name="{esc(name)}">
<summary><span class="entry-name">{esc(name.removeprefix('hamingjur.'))}</span>
<span class="entry-kind">{esc(entry['kind'])}</span><span class="entry-brief">{esc(entry['summary'])}</span></summary>
<div class="entry-content"><div class="entry-tools"><a href="#{anchor(name)}">Permalink</a>
<a href="{esc(source)}" target="_blank" rel="noopener">Source ↗</a></div>
<pre class="signature"><code>{esc(entry['signature'])}</code></pre>{doc}{fields}</div></details>'''


def render_modules(modules):
    """Keep every source entry; filtering changes visibility without dropping results."""
    sections = []
    for module in modules:
        entries = '\n'.join(render_entry(e) for e in module['entries'])
        module_id = module['name'].removeprefix('hamingjur.').replace('.', '-')
        sections.append(f'''<details class="api-module" data-module="{esc(module['name'])}"
id="module-{esc(module_id)}">
<summary><code>{esc(module['name'])}</code><span>{len(module['entries'])} entries</span></summary>
<div class="module-content"><p class="module-description">{esc(module['doc'])}</p>{entries}</div></details>''')
    return '\n'.join(sections)


def main():
    """Write a portable HTML page and an identical static hosting entrypoint."""
    project = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package-root', type=Path, default=project.parents[1])
    parser.add_argument('--export', type=Path, help='also write a standalone HTML file here')
    args = parser.parse_args()
    if not (args.package_root / 'hamingjur' / '__init__.py').is_file():
        parser.error('--package-root must contain the hamingjur package')
    modules = collect(args.package_root)
    tree = ast.parse((args.package_root / 'hamingjur' / '__init__.py').read_text(encoding='utf-8'))
    version = next(ast.literal_eval(n.value) for n in tree.body
                   if isinstance(n, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == '__version__' for t in n.targets))
    page = (project / 'template.html').read_text(encoding='utf-8')
    replacements = {'API_REFERENCE': render_modules(modules), 'VERSION': esc(version),
                    'ENTRY_COUNT': str(sum(len(m['entries']) for m in modules)),
                    'MODULE_COUNT': str(len(modules)),
                    'MODULE_OPTIONS': '\n'.join(f'<option value="{esc(m["name"])}">'
                                                f'{esc(m["name"])}</option>' for m in modules)}
    for key, value in replacements.items():
        page = page.replace('{{' + key + '}}', value)
    if re.search(r'\{\{[A-Z_]+\}\}', page):
        raise ValueError('Unresolved template placeholders')
    destinations = [project / 'index.html', project / 'dist' / 'index.html']
    if args.export:
        destinations.append(args.export)
    for destination in destinations:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(page, encoding='utf-8')
    print(json.dumps({'modules': len(modules), 'entries': sum(len(m['entries']) for m in modules),
                      'bytes': len(page.encode('utf-8')), 'outputs': list(map(str, destinations))}))


if __name__ == '__main__':
    main()

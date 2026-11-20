"""Rasterize the portable subset of Silkie vector scenes with resvg."""
from __future__ import annotations

import math
import re
from collections import defaultdict
from xml.etree import ElementTree
from xml.sax.saxutils import quoteattr

from .protocols import integer

Record = tuple[str, dict[str, str]]
Records = dict[int, Record]
_NUMBER = r'[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?'
_PATH_TOKEN = re.compile(r'[MLHVCSQTZmlhvcsqtz]|' + _NUMBER + r'|,')
_PATH_ARGUMENTS = dict(M=2, L=2, H=1, V=1, C=6, S=4, Q=4, T=2, Z=0)
_PATH_FIELDS = dict(d='d', w='stroke-width', cap='stroke-linecap', join='stroke-linejoin',
                    ml='stroke-miterlimit', dash='stroke-dasharray', dp='stroke-dashoffset')
_RECORD_FIELDS = {
    'g': {'p', 't', 'box', 'o', 'blend', 'v'},
    'p': {'g', 't', 'fill', 'stroke', 'rule', *_PATH_FIELDS},
    's': {'d'},
    'u': {'g', 's', 't', 'fill', 'stroke', 'w'},
}


def _numbers(value: str, count: int) -> list[float]:
    values = [float(part) for part in value.split(',')]
    if len(values) != count or not all(map(math.isfinite, values)):
        raise ValueError('Invalid vector coordinates')
    return values


def _identifier(value: str) -> int:
    return integer(int(value), 'vector element ID', 0, 0xffffffff)


def _delete(records: Records, fields: dict[str, str]) -> None:
    if fields.keys() == {'id'}:
        records.pop(_identifier(fields['id']), None)
    elif fields.keys() == {'g'}:
        children = _children(records)
        pending = [_identifier(fields['g'])]
        seen = set()
        while pending:
            element = pending.pop()
            if element not in seen:
                seen.add(element)
                records.pop(element, None)
                pending.extend(children.get(element, ()))
    else:
        raise ValueError('Invalid vector deletion')


def edit(data: bytes, current: Records | None = None) -> Records:
    """Apply records to a copy so a failed render preserves the previous scene."""
    records = {} if current is None else current.copy()
    for line in data.decode('utf-8').splitlines():
        parts = line.split()
        if not parts:
            continue
        kind = parts.pop(0)
        if kind == 'x' and parts == ['all']:
            records.clear()
            continue
        fields = dict(part.split('=', 1) for part in parts)
        if kind == 'x':
            _delete(records, fields)
            continue
        if kind == 'a':
            element = _identifier(fields.pop('id'))
            target, previous = records[element]
            if target not in {'s', 'p'} or fields.keys() != {'d'}:
                raise NotImplementedError('Only path and symbol geometry can be appended locally')
            if fields['d'] and not fields['d'].startswith('M'):
                raise ValueError('Appended vector geometry must start with an absolute move')
            _validate_path(fields['d'])
            records[element] = target, {**previous, 'd': previous['d'] + fields['d']}
            continue
        if kind not in _RECORD_FIELDS:
            raise NotImplementedError(f'Vector record {kind!r} requires native Silkie')
        element = _identifier(fields.pop('id'))
        unsupported = fields.keys() - _RECORD_FIELDS[kind]
        if unsupported:
            raise NotImplementedError(f'Vector fields require native Silkie: {sorted(unsupported)}')
        if element == 0 and (kind != 'g' or 'p' in fields):
            raise ValueError('Vector ID 0 is reserved for the root group')
        records[element] = kind, fields
    return records


def _children(records: Records) -> dict[int, list[int]]:
    children: dict[int, list[int]] = defaultdict(list)
    for element, (kind, fields) in records.items():
        if element and kind != 's':
            parent = _identifier(fields.get('p' if kind == 'g' else 'g', '0'))
            children[parent].append(element)
    return children


def _attributes(fields: dict[str, str]) -> str:
    return ''.join(f' {key}={quoteattr(value)}' for key, value in fields.items())


def _paint(attrs: dict[str, str], key: str, value: str) -> None:
    if value != 'none' and not re.fullmatch(r'#[a-fA-F0-9]{6}(?:[a-fA-F0-9]{2})?', value):
        raise NotImplementedError(f'Vector paint {value!r} requires native Silkie')
    if len(value) == 9:
        attrs[key + '-opacity'] = str(int(value[-2:], 16) / 255)
        value = value[:7]
    attrs[key] = value


def _validate_path(data: str) -> None:
    """Check SVG commands, including repeated coordinate groups, before rasterization."""
    offset = count = arity = 0
    comma = False
    for part in _PATH_TOKEN.finditer(data):
        value = part.group()
        if part.start() != offset:
            raise ValueError('Invalid vector path')
        if value.upper() in _PATH_ARGUMENTS:
            if (offset == 0 and value.upper() != 'M') or comma or (arity and (
                    count == 0 or count % arity)):
                raise ValueError('Incomplete vector path command')
            arity, count = _PATH_ARGUMENTS[value.upper()], 0
        elif value == ',':
            if not count or comma:
                raise ValueError('Invalid vector path separator')
            comma = True
        else:
            if not arity or not math.isfinite(float(value)):
                raise ValueError('Invalid vector path coordinate')
            count += 1
            comma = False
        offset = part.end()
    if offset != len(data) or comma or (arity and (count == 0 or count % arity)):
        raise ValueError('Incomplete vector path data')


def _path(data: str, fields: dict[str, str], attrs: dict[str, str]) -> str:
    _validate_path(data)
    attrs.update({_PATH_FIELDS[key]: fields[key] for key in _PATH_FIELDS.keys() & fields.keys()})
    attrs['d'] = data
    attrs['fill-rule'] = {'nz': 'nonzero', 'eo': 'evenodd'}[fields.get('rule', 'nz')]
    _paint(attrs, 'fill', fields.get('fill', 'none' if 'stroke' in fields else '#000000'))
    if 'stroke' in fields:
        _paint(attrs, 'stroke', fields['stroke'])
    return '<path' + _attributes(attrs) + '/>'


def _group(element: int, fields: dict[str, str], attrs: dict[str, str]) -> str:
    if fields.get('blend', 'normal') != 'normal':
        raise NotImplementedError('Vector blend modes require native Silkie')
    opacity = _numbers(fields.get('o', '100'), 1)[0]
    if not 0 <= opacity <= 100 or fields.get('v', '1') not in {'0', '1'}:
        raise ValueError('Invalid group opacity or visibility')
    attrs['opacity'] = str(opacity / 100 if fields.get('v') != '0' else 0)
    clip = ''
    if 'box' in fields:
        x, y, width, height = _numbers(fields['box'], 4)
        if width < 0 or height < 0:
            raise ValueError('Invalid vector clip dimensions')
        clip = (f'<defs><clipPath id="box{element}"><rect x="{x}" y="{y}" '
                f'width="{width}" height="{height}"/></clipPath></defs>')
        attrs['clip-path'] = f'url(#box{element})'
    return clip + '<g' + _attributes(attrs) + '>'


def _svg(records: Records, width: int, height: int) -> str:
    """Walk the ordered group tree without a Python recursion limit."""
    children = _children(records)
    for parent in children.keys() - {0}:
        if parent not in records or records[parent][0] != 'g':
            raise ValueError(f'Unknown vector group {parent}')
    output = []
    pending: list[int | None] = [0]
    count = 0
    while pending:
        element = pending.pop()
        if element is None:
            output.append('</g>')
            continue
        kind, fields = records.get(element, ('g', {}))
        count += 1
        attrs = {}
        if 't' in fields:
            matrix = ' '.join(map(str, _numbers(fields['t'], 6)))
            attrs['transform'] = f'matrix({matrix})'
        if kind == 'g':
            output.append(_group(element, fields, attrs))
            pending.append(None)
            pending.extend(reversed(children.get(element, ())))
        elif kind == 'p':
            output.append(_path(fields['d'], fields, attrs))
        elif kind == 'u':
            symbol, definition = records.get(_identifier(fields['s']), ('', {}))
            if symbol == 's':
                output.append(_path(definition['d'], fields, attrs))
    if count != 1 + sum(map(len, children.values())):
        raise ValueError('Cyclic vector group hierarchy')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}">' + ''.join(output) + '</svg>')


def rasterize(records: Records, width: int, height: int) -> bytes:
    """Render a portable scene to PNG with the scene's intrinsic dimensions."""
    from resvg_py import svg_to_bytes  # pylint: disable=import-outside-toplevel

    return bytes(svg_to_bytes(svg_string=_svg(records, width, height)))


class _StaticSVG(ElementTree.TreeBuilder):
    """Reject document types, matching Silkie's static SVG resource policy."""

    def doctype(self, name: str, public_id: str | None, system_id: str | None) -> None:
        """Reject DTDs before the parser can expand a document's entities."""
        raise ValueError('SVG graphics do not accept document type declarations')


def rasterize_svg(data: bytes, width: int = 0, height: int = 0) -> bytes:
    """Rasterize UTF-8 SVG with local fragments and embedded raster images only."""
    from resvg_py import svg_to_bytes  # pylint: disable=import-outside-toplevel

    document = data.decode('utf-8')
    parser = ElementTree.XMLParser(target=_StaticSVG())
    try:
        root = ElementTree.fromstring(document, parser)
    except ElementTree.ParseError as exc:
        raise ValueError(f'Invalid SVG document: {exc}') from exc
    if root.tag not in {'svg', '{http://www.w3.org/2000/svg}svg'}:
        raise ValueError('Expected an SVG document')
    changed = False
    for element in root.iter():
        for key in ('href', '{http://www.w3.org/1999/xlink}href'):
            reference = element.get(key, '').strip()
            if reference and not (reference.startswith('#') or re.match(
                    r'data:image/(?:png|jpeg|gif|webp)[;,]', reference, re.IGNORECASE)):
                del element.attrib[key]
                changed = True
    if changed or root.tag == 'svg':
        if root.tag == 'svg':
            root.set('xmlns', 'http://www.w3.org/2000/svg')
        document = ElementTree.tostring(root, encoding='unicode')
    if bool(width) != bool(height):
        raise ValueError('Specify both SVG dimensions, or neither')
    return bytes(svg_to_bytes(svg_string=document, width=width or None, height=height or None))

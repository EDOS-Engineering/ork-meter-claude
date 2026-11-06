"""Bounded lossless views of complete JSON prefixes. Experimental; no I/O."""
import copy
import hashlib
import json
import re
import time
from dataclasses import dataclass
from json_selection import ExactNumber, encode, _pairs, _constant

FORMAT = 'lossless-json-documents-base-and-changes-v1'
MAX_BYTES = 128 * 1024
MAX_DOCUMENTS = 8
MAX_NODES = 50000
MAX_DEPTH = 64
_SEPARATOR = re.compile(r'[ \t\r\n]*(?:[=-]{3,80}[ \t]*\r?\n[ \t\r\n]*)*')
_DECODER = json.JSONDecoder(object_pairs_hook=_pairs, parse_int=ExactNumber,
                            parse_float=ExactNumber, parse_constant=_constant)

class BudgetExceeded(ValueError):
    pass

def _check(deadline):
    if deadline is not None and time.monotonic() >= deadline:
        raise BudgetExceeded('formatting deadline')

@dataclass(frozen=True)
class Prefix:
    leading: str
    documents: list
    separators: list
    suffix: str

@dataclass(frozen=True)
class View:
    text: str
    kind: str
    document_count: int
    original_sha256: str
    bytes_before: int
    bytes_after: int

def parse_prefix(text, deadline=None):
    if not isinstance(text, str):
        return None
    raw = text.encode('utf-8')
    if not 4096 <= len(raw) <= MAX_BYTES:
        return None
    start = len(text)-len(text.lstrip(' \t\r\n'))
    pos = start
    end = start
    docs, separators = [], []
    while pos < len(text) and text[pos] in '[{':
        _check(deadline)
        if len(docs) == MAX_DOCUMENTS:
            return None
        value, next_end = _DECODER.raw_decode(text, pos)
        if not isinstance(value, (dict, list)):
            return None
        if docs:
            separators.append(text[end:pos])
        docs.append(value)
        end = next_end
        pos = _SEPARATOR.match(text, end).end()
    if not docs:
        return None
    pending = [(v, 0) for v in docs]
    count = 0
    while pending:
        value, depth = pending.pop()
        count += 1
        if count > MAX_NODES or depth > MAX_DEPTH:
            return None
        if count % 256 == 0:
            _check(deadline)
        if isinstance(value, dict):
            pending.extend((v, depth+1) for v in value.values())
        elif isinstance(value, list):
            pending.extend((v, depth+1) for v in value)
    return Prefix(text[:start], docs, separators, text[end:])

def compact(prefix):
    return prefix.leading + ''.join(encode(doc)+(prefix.separators[i]
        if i < len(prefix.separators) else '')
        for i, doc in enumerate(prefix.documents)) + prefix.suffix

def frame(prefix, deadline=None):
    records = []
    for index, doc in enumerate(prefix.documents):
        _check(deadline)
        selected = {'value': doc}
        if isinstance(doc, dict):
            encoded_values = {k: encode(v) for k, v in doc.items()}
            for base_index, base in enumerate(prefix.documents[:index]):
                _check(deadline)
                if not isinstance(base, dict):
                    continue
                changed = {k:v for k,v in doc.items()
                    if k not in base or encoded_values[k] != encode(base[k])}
                removed = [k for k in base if k not in doc]
                candidate = dict(base_document=base_index, set=changed,
                                 remove=removed, key_order=list(doc))
                if len(encode(candidate)) < len(encode(selected)):
                    selected = candidate
        records.append(selected)
    return dict(format=FORMAT, documents=records, separators=prefix.separators)

def reconstruct(value):
    if not isinstance(value, dict) or set(value) != {'format','documents','separators'} or value['format'] != FORMAT:
        raise ValueError('view format')
    records = value['documents']
    if not isinstance(records, list) or not 1 <= len(records) <= MAX_DOCUMENTS:
        raise ValueError('document count')
    separators = value['separators']
    if not isinstance(separators,list) or len(separators) != len(records)-1 or not all(isinstance(s,str) for s in separators):
        raise ValueError('separators')
    docs = []
    for record in records:
        if not isinstance(record,dict):
            raise ValueError('document record')
        if set(record) == {'value'}:
            doc = copy.deepcopy(record['value'])
        else:
            if set(record) != {'base_document','set','remove','key_order'}:
                raise ValueError('change record')
            index = record['base_document']
            if isinstance(index,ExactNumber):
                if not re.fullmatch(r'0|[1-9][0-9]*',index.text):
                    raise ValueError('base index')
                index = int(index.text)
            if type(index) is not int or not 0 <= index < len(docs) or not isinstance(docs[index],dict):
                raise ValueError('base reference')
            changes, removed, order = record['set'],record['remove'],record['key_order']
            if not isinstance(changes,dict) or not isinstance(removed,list) or not isinstance(order,list):
                raise ValueError('change fields')
            if not all(isinstance(k,str) for k in removed+order) or len(removed)!=len(set(removed)):
                raise ValueError('key type or removal')
            doc = copy.deepcopy(docs[index])
            for key in removed:
                if key not in doc or key in changes:
                    raise ValueError('removal target')
                del doc[key]
            doc.update(copy.deepcopy(changes))
            if len(order)!=len(set(order)) or set(order)!=set(doc):
                raise ValueError('key order')
            doc = {key:doc[key] for key in order}
        if not isinstance(doc,(dict,list)):
            raise ValueError('document root')
        docs.append(doc)
    return docs

def choose(text, measure, deadline=None, *, allow_factor=True):
    """Choose only a strictly smaller, round-trip-verified view; otherwise abstain."""
    try:
        _check(deadline)
        prefix = parse_prefix(text,deadline)
        if prefix is None:
            return None
        original = measure(text)
        if type(original) not in (int,float):
            return None
        options = [(compact(prefix),'minified')]
        if allow_factor and len(prefix.documents)>1:
            candidate = frame(prefix,deadline)
            encoded = encode(candidate)
            recovered = reconstruct(_DECODER.decode(encoded))
            if [encode(v) for v in recovered] != [encode(v) for v in prefix.documents]:
                raise ValueError('round-trip mismatch')
            options.append((prefix.leading+encoded+prefix.suffix,'base-and-changes'))
        chosen = None
        score = original
        for result,kind in options:
            _check(deadline)
            result.encode('utf-8')
            measured = measure(result)
            if type(measured) not in (int,float):
                continue
            if measured < score:
                score = measured
                chosen = (result,kind)
        _check(deadline)
        if chosen is None:
            return None
        result,kind = chosen
        return View(result,kind,len(prefix.documents),hashlib.sha256(text.encode('utf-8')).hexdigest(),
                    len(text.encode('utf-8')),len(result.encode('utf-8')))
    except (ValueError,RecursionError,UnicodeError,OverflowError,TypeError):
        return None

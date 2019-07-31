import pandas as pd
import numpy as np
import json
from pathlib import Path
import re
from dataclasses import dataclass, field
from typing import List
from ..helpers.jupyter import display_table

@dataclass
class Cell:
    value: str
    raw_value: str
    gold_tags: str = ''
    refs: List[str] = field(default_factory=list)
    layout: str = ''


reference_re = re.compile(r"<ref id='([^']*)'>(.*?)</ref>")
num_re = re.compile(r"^\d+$")

def extract_references(s):
    parts = reference_re.split(s)
    refs = parts[1::3]
    text = []
    for i, x in enumerate(parts):
        if i % 3 == 0:
            text.append(x)
        elif i % 3 == 2:
            s = x.strip()
            if num_re.match(s):
                text.append(s)
            else:
                text.append(f"[{s}]")
    text = ''.join(text)
    return text, refs


def str2cell(s):
    value, refs = extract_references(s)
    return Cell(value=value, raw_value=s, refs=refs)

def read_str_csv(filename):
    try:
        df = pd.read_csv(filename, header=None, dtype=str).fillna('')
    except pd.errors.EmptyDataError:
        df = pd.DataFrame()
    return df




class Table:
    def __init__(self, df, layout, caption=None, figure_id=None, annotations=None, old_name=None, guessed_tags=None):
        self.df = df
        self.caption = caption
        self.figure_id = figure_id
        self.df = df.applymap(str2cell)
        self.old_name = old_name

        if layout is not None:
            #self.layout = layout
            for r, row in layout.iterrows():
                for c, cell in enumerate(row):
                    self.df.iloc[r,c].layout = cell

        if annotations is not None:
            self.gold_tags = annotations.gold_tags.strip()
            self.dataset_text = annotations.dataset_text.strip()
            self.notes = annotations.notes.strip()
            if guessed_tags is not None:
                tags = guessed_tags.values
            else:
                tags = annotations.matrix_gold_tags
            gt_rows = len(tags)
            if gt_rows == 0 and len(self.df) > 0:
                #print(f"Gold tags size mismatch: 0 vs {len(self.df)} in old name {old_name}")
                self.old_name = None
            elif gt_rows > 0:
                gt_cols = len(tags[0])
                if self.df.shape != (0,0) and self.df.shape == (gt_rows, gt_cols):
                    for r, row in enumerate(tags):
                        for c, cell in enumerate(row):
                            self.df.iloc[r,c].gold_tags = cell.strip()
                else:
                    if guessed_tags is not None:
                        print(f"Gold tags size mismatch: {gt_rows},{gt_cols} vs {self.df.shape}")
                #    print(f"Gold tags size mismatch: {gt_rows},{gt_cols} vs {self.df.shape}")
                #    print(annotations.matrix_gold_tags)
                #    print(self.df.applymap(lambda c:c.value))
                    self.old_name = None
        else:
            self.gold_tags = ''
            self.dataset_text = ''
            self.notes = ''

    @classmethod
    def from_file(cls, path, metadata, annotations=None, match_name=None, guessed_tags=None):
        path = Path(path)
        filename = path / metadata['filename']
        df = read_str_csv(filename)
        if 'layout' in metadata:
            layout = read_str_csv(path / metadata['layout'])
        else:
            layout = None
        if annotations is not None and match_name is not None:
            table_ann = annotations.table_set.filter(name=match_name) + [None]
            table_ann = table_ann[0]
        else:
            table_ann = None
        return cls(df, layout, metadata.get('caption'), metadata.get('figure_id'), table_ann, match_name, guessed_tags)

    def display(self):
        display_table(self.df.applymap(lambda x: x.value).values, self.df.applymap(lambda x: x.gold_tags).values)

#####
# this code is used to migrate table annotations from
# tables parsed by htlatex to tables parsed by
# latexml. After all annotated tables will be successfully
# migrated, we switch back to match-by-name

from unidecode import unidecode
import string
from collections import Counter

figure_prefix_re = re.compile('^(table|figure)\s+([0-9]+|[ivxl]+)?')
punctuation_table = str.maketrans('', '', string.punctuation)
def normalize_string(s):
    if s is None:
        return ""

    s = s.strip().lower()
    s = figure_prefix_re.sub('', s).strip()
    return unidecode(s.replace('\xa0', '').replace(' ', '')).translate(punctuation_table)

def _remove_almost_empty_values(d):
    return {k:v for k,v in d.items() if len(v) >= 10}

def _keep_unique_values(d):
    c = Counter(d.values())
    unique = [k for k,v in c.items() if v == 1]
    return {k: v for k,v in d.items() if v in unique}

def _match_tables_by_captions(annotations, metadata):
    if annotations is None:
        return {}
    old_captions = {x.name: normalize_string(x.desc) for x in annotations.table_set}
    new_captions = {m['filename']: normalize_string(m['caption']) for m in metadata}
    old_captions = _keep_unique_values(_remove_almost_empty_values(old_captions))
    new_captions = _keep_unique_values(_remove_almost_empty_values(new_captions))
    old_captions_reverse = {v:k for k,v in old_captions.items()}
    return {new_name:old_captions_reverse[caption] for new_name, caption in new_captions.items() if caption in old_captions_reverse}

def normalize_cell(s):
    #s = reference_re.sub(' [] ', s)
    return normalize_string(s)

# begin of guess annotations mapping
def create_cell_contexts(df):
    cell_context = df.values
    cells = np.pad(cell_context, 1, mode='constant', constant_values='')

    slices = [slice(None, -2), slice(1,-1), slice(2, None)]

    row_context = np.stack([cells[1:-1, s] for s in slices], axis=-1)
    col_context = np.stack([cells[s, 1:-1] for s in slices], axis=-1)
    box_context = np.stack([cells[s1, s2] for s1 in slices for s2 in slices], axis=-1)
    return box_context, row_context, col_context, cell_context[...,None]

def map_context(context, values):
    ctx_len = context.shape[-1]
    mapping = {}
    for ctx, val in zip(context.reshape((-1, ctx_len)), values.reshape(-1)):
        mapping.setdefault(tuple(ctx), set()).add(val)
    return mapping

REANNOTATE_TAG = 'reannotate'

def guess_annotations(old_table, gold_tags, new_table):
    df = pd.DataFrame().reindex_like(new_table).fillna(REANNOTATE_TAG)
    if old_table.empty:
        return 0, df
    old_contexts = create_cell_contexts(old_table)
    old_mappings = [map_context(ctx, gold_tags.values) for ctx in old_contexts]
    new_contexts = create_cell_contexts(new_table)

    rows, cols = new_table.shape
    matched = 0
    for row in range(rows):
        for col in range(cols):
            for mapping, context in zip(old_mappings, new_contexts):
                ctx = tuple(context[row, col])
                values = mapping.get(ctx, set())
                if len(values) == 1:
                    (val,) = values
                    df.iloc[row, col] = val
                    matched += 1
                    break
    return matched, df

# end of guess annotations mapping


def same_table(old_table, new_table):
    return old_table.equals(new_table)

DEB_PAPER="1607.00036v2"

def deb(path, old_name, old_table, new_name, new_table):
    if path.name == DEB_PAPER and old_name == "table_02.csv" == new_name:
        print(old_table)
        print(new_table)

def _match_tables_by_content(path, annotations, metadata):
    if annotations is None:
        return {}, {}
    old_tables = {x.name: (pd.DataFrame(x.matrix).applymap(normalize_cell), pd.DataFrame(x.matrix_gold_tags)) for x in annotations.table_set}
    new_tables = {m['filename']: Table.from_file(path, m, None, None).df.applymap(lambda c: normalize_cell(c.value)) for m in metadata}
    matched = {}
    new_tags = {}
    for new_name, new_table in new_tables.items():
        max_hits = 0
        matched_name = None
        size = np.prod(new_table.shape)
        guessed_tags = None
        for old_name, (old_table, gold_tags) in old_tables.items():
            hits, tags = guess_annotations(old_table, gold_tags, new_table)
            if hits > max_hits:
                max_hits = hits
                matched_name = old_name
                guessed_tags = tags
        if max_hits > size / 2:
            matched[new_name] = matched_name
            new_tags[new_name] = guessed_tags
            #deb(path, old_name, old_table, new_name, new_table)
            #if same_table(old_table, new_table):
            #    if new_name in matched:
            #        print(f"Multiple matches for {path}/{new_name}: {matched[new_name]}, {old_name}")
            #    else:
            #        matched[new_name] = old_name
    return matched, new_tags
####

def read_tables(path, annotations):
    path = Path(path)
    with open(path / "metadata.json", "r") as f:
        metadata = json.load(f)
    _matched_names_by_captions = {} #_match_tables_by_captions(annotations, metadata)
    _matched_names_by_content, _guessed_tags = _match_tables_by_content(path, annotations, metadata)
    _matched_names = _matched_names_by_captions
    for new_name, old_name in _matched_names_by_content.items():
        if new_name in _matched_names and _matched_names[new_name] != old_name:
            print(f"Multiple matches for table {path}/{new_name}: {_matched_names[new_name]} by caption and {old_name} by content")
        else:
            _matched_names[new_name] = old_name
    return [Table.from_file(path, m, annotations, match_name=_matched_names.get(m["filename"]), guessed_tags=_guessed_tags.get(m["filename"])) for m in metadata]

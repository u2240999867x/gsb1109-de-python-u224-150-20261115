# Example: Diffing two ISO images
Two ISOs can hold exactly the same files while storing them at completely
different offsets.  Re-mastering an image moves directory data, file data,
and path tables around even when nothing logical changed.  `pycdlib.diff`
compares the images by identity instead of by layout, so this section walks
through the four kinds of result it reports.

Here is the complete code for the example:

```python
import pycdlib
from pycdlib import diff

result = diff.diff_paths('old.iso', 'new.iso')

for update in result.content_updates:
    print('content update:', update.old.path, '->', update.new.path)
for migration in result.path_migrations:
    print('path migration:', migration.old.path, '->', migration.new.path)
for change in result.attribute_only_changes:
    print('attributes:', change.entry.new.path, change.changes)
for change in result.shared_block_changes:
    print('sharing:', change.change, change.old_paths, '->', change.new_paths)
```

`diff_paths` opens both images, builds a snapshot of one namespace, hashes
each file, and matches entries.  The returned object keeps each category
separate, so a caller can report only what it cares about.

## Choosing the namespace
By default the diff prefers Rock Ridge, then UDF, then Joliet, then ISO9660,
using the first namespace both images provide.  Pass `namespace='iso'` to
compare the short ISO9660 names, or `namespace='joliet'` to compare the Joliet
tree.  Comparing one namespace at a time is usually easier to read: a file
removed from Joliet but left in ISO9660 is a real difference that no single
merged report can express.

```python
result = diff.diff_paths('old.iso', 'new.iso', namespace='iso')
```

## How identity is decided
An entry's identity is proved by two independent kinds of evidence:

- **Directory evidence** -- the same path on both sides, a directory whose
  subtree structure is unchanged, or a unique name under a matched parent
  directory.
- **A content digest** -- the file's bytes hash to the same SHA-256 value.

A same-path pair whose digests differ is a *content update*: the path proves
identity, the digest proves the change.  A different-path pair with an equal
digest is a *path migration*.  A directory whose complete subtree is
byte-identical can be matched even after it was renamed, and a directory whose
relative names and kinds survived a rewrite gives enough evidence to pair a
file that both moved and changed.  When several files share one digest and the
directory evidence cannot choose between them, the entry is listed as
ambiguous instead of guessed at.

## Attribute-only changes
When the path and the digest both match but the directory-record metadata
differs, the entry is reported in `attribute_only_changes`.  This covers
directories, which have no file data of their own, and files whose mode or
ownership changed without their contents changing.

Timestamps are ignored by default, because mastering an unchanged tree through
pycdlib stamps fresh times into every record.  Pass `include_timestamps=True`
to treat them as attributes too.

## Shared data blocks
Hard links and overlapping extents mean two paths read the same bytes.  A
re-master can split that relationship or create a new one without changing any
file content, so `shared_block_changes` reports `separated` and `joined` pairs
separately from the content and path categories.  Sharing is evaluated per
logical block, so two UDF files that start at different offsets inside the same
block count as sharing it.

## Missing or incomplete old images
No conclusion about a deletion can be drawn from a missing old image.  When
`old_path` is `None`, or the old image cannot be opened or read completely,
`diff_paths` leaves `removed` empty, sets `deletion_verified` to `False`, and
lists unmatched entries under `unverified`.  The report says explicitly that
deletions were not inferred.  If the caller knows the old image is incomplete
for another reason, `old_complete=False` forces the same conservative behavior.

## Machine-readable output
`result.to_dict()` returns only JSON-serializable values, which makes it easy
to feed the diff into a release pipeline.  The `pycdlib-diff` tool does this
for you:

```bash
pycdlib-diff --old old.iso --new new.iso --format json --output report.json
```

Pass `--fail-on-diff` to exit with status 1 when any confirmed change is
found.

---

<div style="width: 100%; display: table;">
  <div style="display: table-row;">
    <div style="width: 33%; display: table-cell; text-align: left;">
      <a href="example-reading-file-in-chunks.html"><-- Example: Reading a large file in chunks</a>
    </div>
    <div style="width: 33%; display: table-cell; text-align: center;">
      <a href="https://clalancette.github.io/pycdlib/">Top</a>
    </div>
    <div style="width: 33%; display: table-cell; text-align: right;">
      <a href="design.html">Design --></a>
    </div>
  </div>
</div>

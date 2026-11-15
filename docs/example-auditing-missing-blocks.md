# Example: Triage files against a missing block table
A repair station can hand over an ISO together with a list of logical blocks
that could not be read from the original media.  Before ingesting any files,
it helps to know which ones are whole, which can only be partially recovered,
and which directories are themselves too damaged to trust.  PyCdlib exposes
that as the [audit_missing_blocks](api/pycdlib.html#pycdlib.pycdlib.PyCdlib.audit_missing_blocks)
API.  Here's the complete code for this example:

```python
import pycdlib
from pycdlib import health

iso = pycdlib.PyCdlib()
iso.open('damaged.iso')

table = health.load_block_table('missing-blocks.txt')
report = iso.audit_missing_blocks(table)

for entry in report.files:
    print(entry.path, entry.status, entry.readable_bytes, entry.size)

for entry in report.directories:
    print(entry.path, entry.status)

iso.close()
```

Let's take a closer look at the code.

```python
import pycdlib
from pycdlib import health
```

`health` contains the block table parser and the result classes.  The audit
itself is a method on `PyCdlib`, so it is also available on each facade.

```python
table = health.load_block_table('missing-blocks.txt')
```

The table file is plain text.  Each line may contain comments starting with
`#` or `//`; entries are separated by commas, semicolons, or whitespace.  A
single block is written as `42` or `0x2a`, and a range is written as `42-50`
or `42..50`.  A `BlockTable` can also be built directly from a list of block
numbers, `range()` objects, and inclusive `(first, last)` tuples.

```python
report = iso.audit_missing_blocks(table)
```

The audit returns a `HealthReport`.  By default it picks a single view the
same way `pycdlib-extract-files` does: UDF when the ISO has it, otherwise
Rock Ridge, otherwise Joliet, otherwise ISO9660.  Pass `views=['iso', 'rr']`
(or any other list) to audit several views at once.  A file gets one of four
statuses:

- `complete`: every byte of the file is readable.  A zero-byte file has no
  data blocks and is always complete, since the table says nothing about it.
- `partial`: some bytes are readable and some are not.  `missing_ranges`
  lists the damaged runs as `(offset, length)` pairs relative to the file.
- `missing`: the file has data, but none of it is readable.
- `untrusted`: the file's own metadata or the directory chain above it is
  damaged.  The records in a damaged directory may be gone, so every entry
  below it is reported this way even if its data blocks look intact.

A directory is `trusted` when all of its own directory blocks are present and
all of its ancestors are trusted; otherwise it is `untrusted` and everything
below it inherits that status.

```python
for entry in report.files:
    print(entry.path, entry.status, entry.readable_bytes, entry.size)
```

`report.files` contains one entry per path per audited view.  Files that
share one data extent (hard links) are all affected in the same way, because
they resolve to the same blocks.

`report` also offers convenience collections: `complete_files`,
`partial_files`, `missing_files`, `untrusted_files`, and
`untrusted_directories`.  `report.summary()` gives the counts, and
`report.to_dict()` is JSON-friendly for recording with the ingested file.

```python
single = iso.file_health(table, iso_path='/FOO.;1')
```

For a single path, [file_health](api/pycdlib.html#pycdlib.pycdlib.PyCdlib.file_health)
returns the same `FileHealth` object without walking the whole image.

```python
report = iso.audit_missing_blocks(table, block_size=512)
```

The block table may be recorded in a different block size than the ISO's
logical block size.  Passing `block_size` interprets the table's numbers in
that unit; for example a table of 512-byte sectors can be checked against a
2048-byte ISO, and only the affected part of a file is reported as missing.

Note that a directory block that is missing may make the ISO impossible to
open at all, for instance when the Primary Volume Descriptor is damaged.  In
that case there is no in-memory filesystem to audit; the `open` call raises
`PyCdlibInvalidISO`.  `audit_missing_blocks` is for images that still open,
where the damage is confined to file data or to directory subtrees.

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
      <a href="examples.html">Examples --></a>
    </div>
  </div>
</div>

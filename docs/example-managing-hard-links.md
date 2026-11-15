# Example: Managing hard-links on an ISO
PyCdlib supports an advanced concept called hard-links, which is multiple names for the same piece of data (this is somewhat similar to Unix hard-links).  Most users will not need to use this functionality and should stick with the standard [add_file](api/pycdlib.html#pycdlib.pycdlib.PyCdlib.add_file) and [rm_file](api/pycdlib.html#pycdlib.pycdlib.PyCdlib.rm_file) APIs.  However, for those that want to do more advanced things like hiding a file from Joliet while having it remain visible in ISO9660, this functionality can be useful.

On an ISO, a piece of data can be referred to (possibly several times) from four different contexts:

1.  From the original ISO9660 context, including the Rock Ridge extensions.
1.  From the Joliet context, since this is a separate context.
1.  From the El Torito boot record, since this is effectively a separate context.
1.  From the UDF context, since this is a separate context.

The data can be referred to zero, one, or many times from each of these contexts.  The most classic example of hard-links happens when an ISO has the Joliet extensions.  In that case, there is implicitly a hard-link from the ISO9660 (and Rock Ridge) context to the file contents, and a hard-link from the Joliet context to the file contents.  When a piece of data has zero entries in a context, it is effectively hidden from that context.  For example, a file could be visible from ISO9660/Rock Ridge, but hidden from Joliet, or vice-versa.  A file could be used for booting, but be hidden from both ISO9660/Rock Ridge and Joliet, etc.  Management of these hard-links is done via the PyCdlib APIs [add_hard_link](api/pycdlib.html#pycdlib.pycdlib.PyCdlib.add_hard_link) and [rm_hard_link](api/pycdlib.html#pycdlib.pycdlib.PyCdlib.rm_hard_link).  Adding or removing a file through the [add_file](api/pycdlib.html#pycdlib.pycdlib.PyCdlib.add_file) and [rm_file](api/pycdlib.html#pycdlib.pycdlib.PyCdlib.rm_file) APIs implicitly manipulates hard-links behind the scenes.  Note that hard-links only make sense for files, since directories have no direct data (only metadata).

An example should help to illustrate the concept.  Here's the complete code for the example:

```python
from io import BytesIO

import pycdlib

iso = pycdlib.PyCdlib()
iso.new(joliet=3)

foostr = b'foo\n'
iso.add_fp(BytesIO(foostr), len(foostr), '/FOO.;1', joliet_path='/foo')

iso.add_hard_link(iso_old_path='/FOO.;1', iso_new_path='/BAR.;1')

iso.rm_hard_link(joliet_path='/foo')

outiso = BytesIO()
iso.write_fp(outiso)

iso.close()
```

Let's take a closer look at the code.

```python
from io import BytesIO

import pycdlib
```

As in earlier examples, import the relevant libraries, including pycdlib itself.

```python
iso = pycdlib.PyCdlib()
iso.new(joliet=3)
```

As in earlier examples, create a PyCdlib object, and then create a new, empty ISO with the Joliet extensions.

```python
foostr = b'foo\n'
iso.add_fp(BytesIO(foostr), len(foostr), '/FOO.;1', joliet_path='/foo')
```

As in earlier examples, add a new file to the ISO.  Here we have provided both the ISO path '/FOO.;1' and the Joliet path '/foo', so the file implicitly has two links; one from the ISO context, and one from the Joliet context.

```python
iso.add_hard_link(iso_old_path='/FOO.;1', iso_new_path='/BAR.;1')
```

Add a hard-link from the original '/FOO.;1' location in the ISO context to a second location in the ISO context '/BAR.;1'.  This takes up no additional space on the ISO for the data, only for the metadata.

```python
iso.rm_hard_link(joliet_path='/foo')
```

Remove the link from the Joliet context for the file.  Now this file is effectively hidden from the Joliet context, while still being visible in the ISO context.

```python
outiso = BytesIO()
iso.write_fp(outiso)
```

As in earlier examples, write the ISO out to the BytesIO object.

```python
iso.close()
```

Since we are done with the ISO object, close it out.

---

<div style="width: 100%; display: table;">
  <div style="display: table-row;">
    <div style="width: 33%; display: table-cell; text-align: left;">
      <a href="example-modifying-file-in-place.html"><-- Example: Modifying a file in place</a>
    </div>
    <div style="width: 33%; display: table-cell; text-align: center;">
      <a href="https://clalancette.github.io/pycdlib/">Top</a>
    </div>
    <div style="width: 33%; display: table-cell; text-align: right;">
      <a href="example-forcing-consistency.html">Example: Forcing consistency --></a>
    </div>
</div>

## Choosing what a content update does to hard-links
Replacing a file's contents is ambiguous when the data has more than one path: either one path is rebound to the new contents, or every path is.  PyCdlib makes that choice explicit rather than guessing.

```python
iso.update_file_contents_fp(io.BytesIO(b'bar\n'), 4, iso_path='/BAR.;1', mode='detach')
```

With `mode='detach'`, only `/BAR.;1` starts pointing at `bar\n`; the `/FOO.;1` link keeps `foo\n`.  With `mode='replace'` (the default used by earlier releases), every link sees the new contents.

To inspect the consequences first, build a plan instead of applying the update directly.  The plan lists the paths that change, the paths that keep the old contents, and whether the old data is reclaimed because its last reference is going away:

```python
plan = iso.plan_update(io.BytesIO(b'bar\n'), 4, mode='detach', iso_path='/BAR.;1')
print(plan)
plan.apply()
```

Data is reclaimed only when an update removes its last reference.  A `detach` that leaves other links behind therefore keeps the old data alive until those links are removed in turn.

## Reporting alias collisions before writing
Rock Ridge aliases (`rr_path`) and ISO9660 names share a directory entry, and deep-directory relocation can rewrite an ISO9660 name when the requested name collides inside `RR_MOVED`.  Both cases make some paths ambiguous, so PyCdlib reports them *before* any image bytes are written:

```python
for collision in iso.find_alias_collisions():
    print(collision)
```

`write()` and `write_fp()` perform the same scan automatically and emit a `UserWarning` that lists every affected path (ISO9660, physical ISO9660, Rock Ridge, and Joliet as applicable).  Construct the object with `PyCdlib(strict_alias_collisions=True)` to make that report an exception instead, or pass `report_alias_collisions=False` to skip it.

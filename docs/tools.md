# Tools
PyCdlib comes with several tools built on the main library API.  These tools are useful in themselves, but also give (relatively) simple examples on how to use the PyCdlib API in a real application.

## pycdlib-genisoimage
The `pycdlib-genisoimage` tool aims to be a flag-compatible drop-in replacement for the venerable [genisoimage](https://linux.die.net/man/1/genisoimage) tool, with the big exception that it uses PyCdlib under the hood.  Please see the man page [pycdlib-genisoimage](pycdlib-genisoimage.html) for more information.

## pycdlib-explorer
The `pycdlib-explorer` tool gives the user a convenient, shell-like interface for opening, exploring, and manipulating existing ISO files.  Please see the man page [pycdlib-explorer](pycdlib-explorer.html) for more information.

## pycdlib-extract-files
The `pycdlib-extract-files` tool is a convenient way to extract particular files or directories from an ISO (including the entire ISO).  Please see the man page [pycdlib-extract-files](pycdlib-extract-files.html) for more information.

## pycdlib-salvage
The `pycdlib-salvage` tool triages an ISO that arrived with a bad-block (missing-sector) table.  It cross-references each entry's extents against the table and classifies every file as complete or partially readable, and every directory whose own blocks are damaged as untrusted.  Files that share a bad block (hard links, ISO9660/UDF bridges) are all affected, while zero-length files are never treated as missing.  The tool can print the assessment or ingest the still-safe entries to disk.  Please see the man page [pycdlib-salvage](pycdlib-salvage.html) for more information.

---

<div style="width: 100%; display: table;">
  <div style="display: table-row;">
    <div style="width: 33%; display: table-cell; text-align: left;">
      <a href="design.html"><-- Design</a>
    </div>
    <div style="width: 33%; display: table-cell; text-align: center;">
      <a href="https://clalancette.github.io/pycdlib/">Top</a>
    </div>
    <div style="width: 33%; display: table-cell; text-align: right;">
      <a href="exceptions.html">Exceptions --></a>
    </div>
</div>

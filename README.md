python-evtx
===========

Introduction
------------

python-evtx is a pure Python parser for recent Windows Event Log files (those with the file extension ".evtx").  The module provides programmatic access to the File and Chunk headers, record templates, and event entries.  For example, you can use python-evtx to review the event logs of Windows 7 systems from a Mac or Linux workstation. The structure definitions and parsing strategies were heavily inspired by the work of Andreas Schuster and his Perl implementation "Parse-Evtx".

Background
----------
With the release of Windows Vista, Microsoft introduced an updated event log file format.  The format used in Windows XP was a circular buffer of record structures that each contained a list of strings.  A viewer resolved templates hosted in system library files and inserted the strings into appropriate positions.  The newer event log format is proprietary binary XML.  Unpacking chunks from an event log file from Windows 7 results in a complete XML document with a variable schema.  The changes helped Microsoft tune the file format to real-world uses of event logs, such as long running logs with hundreds of megabytes of data, and system independent template resolution.

Related Work
------------
Andreas Schuster released the first public description of the .evtx file format in 2007.  He is the author of the thorough document "Introducing the Microsoft Vista event log file format" that describes the motivation and details of the format.  Mr. Schuster also maintains the Perl implementation of a parser called "Parse-Evtx".  I referred to the source code of this library extensively during the development of python-evtx.

Joachim Metz also released a cross-platform, LGPL licensed C++ based parser in 2011.  His document "Windows XML Event Log (EVTX): Analysis of EVTX" provides a detailed description of the structures and context of newer event log files.

Dependencies
------------
python-evtx is a pure Python 3 module, so it works equally well across platforms like Windows, macOS, and Linux. 

python-evtx operates on event log files from Windows operating systems newer than Windows Vista.  These files typically have the file extension .evtx.  Version 5.09 of the `file` utility identifies such a file as "MS Vista Windows Event Log".  To manual confirm the file type, look for the ASCII string "ElfFile" in the first seven bytes:

    willi/evtx  » xxd -l 32 Security.evtx 
    0000000: 456c 6646 696c 6500 0000 0000 0000 0000  ElfFile.........
    0000010: d300 0000 0000 0000 375e 0000 0000 0000  ........7^......


Examples
--------
Provided with the parsing module `Evtx` are four scripts that mimic the tools distributed with Parse-Evtx.  `evtx_info.py` prints metadata about the event log and verifies the checksums of each chunk.  `evtx_templates.py` builds and prints the templates used throughout the event log. `evtx_dump.py` parses the event log and transforms the binary XML into a human readable ASCII XML format. Finally, `evtx_dump_json.py` parses event logs, similar to `evtx_dump.py` and transforms the binary XML into JSON with the added capability to output the JSON array to a file. 

Note the length of the `evtx_dump.py` script: its only 20 lines.  Now, review the contents and notice the complete implementation of the logic:

    print(e_views.XML_HEADER)
    print('<Events>')
    for record in log.records:
        print(record.xml())
    print('</Events>')  

Working with python-evtx is really easy!


Event fields as a long table
----------------------------
`Evtx.Fields` exports the `EventData` / `UserData` body of each event as one
row per binary-XML element.  The rows are fixed-column and stable, so the same
EVTX file always produces the same table.  The export walks the parsed nodes
directly, so it keeps:

- the namespace and prefix of every element and attribute,
- the document order (including repeated `Data` names), with a unique `path`
  such as `/EventData[1]/Data[2]` and a per-parent `occurrence` counter,
- `name=None` for items that have no `Name` attribute (never an invented name),
- `is_null` (no value at all) separately from `is_empty` (a present empty
  value), plus `empty_element` for self-closing elements,
- the declared binary-XML type: `type_code`, `type_name` (for example
  `WSTRINGARRAY`) and `value_type` (the Python node class), and
- full attribute and `xmlns` declaration data as structured columns.

A value that merely looks like XML stays text.  For example, a `WSTRINGARRAY`
renders as `<string>...</string>` fragments, but it is exported as one value of
type `WSTRINGARRAY`, not expanded into child elements.

```python
import Evtx.Evtx as evtx
import Evtx.Fields as e_fields

with evtx.Evtx("System.evtx") as log:
    for record in log.records():
        rows = record.fields()          # same as e_fields.record_fields(record)
        for row in rows:
            print(row["path"], row["name"], row["type_name"], repr(row["value"]))
```

Rows are dicts (also readable as attributes, e.g. `row.name`).  The table
keeps the original text verbatim, including characters that XML 1.0 cannot
represent.  Rebuilding XML applies the same restricted-character filter as the
normal XML view, so comparison ignores those characters:

```python
xml = e_fields.rebuild_body_xml(rows)   # <EventData>...</EventData>
assert e_fields.verify_roundtrip(record, rows)
```

The `evtx_dump_fields` command writes the same table as CSV, JSON, or JSON
Lines:

    evtx_dump_fields System.evtx --format csv -o system-fields.csv
    evtx_dump_fields System.evtx --records 12050,12091 --format json
    evtx_dump_fields System.evtx --leaves-only --verify --format jsonl
    evtx_dump_fields System.evtx --rebuild 12091


Installation
------------
Updates to python-evtx are pushed to PyPi, so you can install the module using `pip`.  For example:

    pip install python-evtx

The source code for python-evtx is hosted at Github, and you may download, fork, and review it from this repository (http://www.github.com/williballenthin/python-evtx).  Please report issues or feature requests through Github's bug tracker associated with the project.

Development
-----------
For formatting, use isort:
    
    isort --length-sort --profile black --line-length=120 Evtx/ scripts/ tests/

and black:

    black --line-length=120 Evtx/ scripts/ tests/

For linting, use ruff:

    ruff check Evtx/ scripts/ tests/

Or use [just](https://github.com/casey/just) to run the linters:

    just lint

License
-------
python-evtx is licensed under the Apache License, Version 2.0.  This means it is freely available for use and modification in a personal and professional capacity.  


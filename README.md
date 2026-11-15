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

Versioned duration fields
-------------------------
Providers change the meaning of their fields between versions. A duration field
can be declared in milliseconds in one provider version and in microseconds in
the next, and older versions can declare the same field name more than once.
`Evtx.EventTable` turns those declarations into one unified table without
guessing: the caller supplies an explicit `(provider, version)` mapping.

    {
      "canonical_unit": "ms",
      "providers": {
        "Contoso-Provider": {
          "1": {"field": "Duration", "source_unit": "ms", "occurrence": 1},
          "2": {"field": "Duration", "source_unit": "us", "index": 0}
        }
      }
    }

`occurrence` (zero based, among same-name fields) or `index` (position in the
declared field sequence) identifies the declaration to use when a field name is
duplicated; leaving both out is only valid when the name is unique. Each row
records the source field name, index, occurrence, path, declared unit, and the
exact conversion factor, so the conversion can be audited. `duration_ms` is the
default unified column; pass `canonical_unit="us"` to normalize in
microseconds instead.

    import Evtx.Evtx as evtx
    from Evtx.EventTable import EventTable, iter_declared_events

    table = EventTable.from_json_file("duration-mapping.json")
    with evtx.Evtx("System.evtx") as log:
        table.normalize_many(iter_declared_events(log))

    for row in table.rows:
        print(row["duration_ms"], row["migration"]["source_path"])

Three rules are enforced:

- **Unknown versions keep their original declarations.** No wildcard or
  nearest-version fallback is applied, and no canonical value is invented.
- **A missing field is not a numeric zero.** An absent declaration yields
  `None` with status `missing`; a declared `0` yields `0` with status
  `migrated`.
- **Normalization is idempotent.** A row that has already been migrated is
  recognized by its `migration` marker and is not scaled a second time.

The same choices are available on the command line:

    evtx_event_table System.evtx --mapping duration-mapping.json --format jsonl
    evtx_event_table System.evtx --mapping duration-mapping.json --format csv \
        --show-conversions

`CSV` output includes the migration status, source field location, unit, and
factor for each event.


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


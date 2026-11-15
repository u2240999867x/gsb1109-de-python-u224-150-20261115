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

Anonymizing events
------------------
`evtx_anonymize.py` (also `Evtx.Anonymize`) exports events after replacing only
the user identifiers selected by an explicit path policy. It is not a text
search: the same string in an unselected field, such as `Provider/@Name`,
`Computer` or another `Data` entry, is left untouched. A sample policy:

```json
{
  "namespaces": {"e": "http://schemas.microsoft.com/win/2004/08/events/event"},
  "paths": [
    "/e:Event/e:System/e:Security/@UserID",
    "/e:Event/e:EventData/e:Data[@Name='SubjectUserName']/text()",
    "/e:Event/e:EventData/e:Data[@Name='TargetUserName']/text()"
  ],
  "mapping": {"S-1-5-18": "USER-0001", "SYSTEM": "USER-0001"}
}
```

```console
evtx_anonymize security.evtx --policy policy.json -o events.json
evtx_anonymize security.evtx --path "/Event/System/Security/@UserID" -o events.xml
```

Paths support a small XPath subset: element and `@attribute` steps, `text()`,
an element's own text, `//` descendants, `*` wildcards, namespace prefixes and
Clark notation (`{uri}name`), plus `[n]`, `[last()]`, `[position()=n]`,
`[@name]`, `[@name='value']` and `[text()='value']` predicates. Positions are
per parent, so duplicate entries are addressed by occurrence rather than by
value.

The same identifier always receives the same pseudonym across events and
selected paths. Unmapped values are replaced deterministically from a salted
SHA-256 digest, so runs are reproducible without a state database. `mapping`
may send several related originals (for example a SID and its account name) to
one pseudonym, and `relations` groups can bind paths together. Note that
`skip_values` defaults to `""` and `"-"` because these are sentinels, not
identifiers; override it when they must be replaced too.

The exporter verifies every event before writing: tags, attributes and child
shape must be unchanged, so the result still parses and keeps the same
structure. `xmltodict`-style JSON (`--format json` or `jsonl`) retains the full
element hierarchy.


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


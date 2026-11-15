Library usage
#############

Use the :py:class:`~pcapng.scanner.FileScanner` class to iterate over blocks
in a pcap-ng archive file, like this:

.. code-block:: python

    from pcapng import FileScanner

    with open('/tmp/mycapture.pcap') as fp:
        scanner = FileScanner(fp)
        for block in scanner:
            pass  # do something with the block...

Block types can be checked against blocks in :py:mod:`pcapng.blocks`.

Correlating two captures
########################

Captures taken at different vantage points usually have clock offsets, clock
skew, and different snapshot lengths.  The :py:mod:`pcapng.correlation` module
matches packet observations across two captures:

.. code-block:: python

    from pcapng.correlation import correlate, read_observations

    first = read_observations("first.pcapng")
    second = read_observations("second.pcapng")

    result = correlate(
        first,
        second,
        anchors=[(1000.000, 900.000), (6100.000, 6000.000)],
        window=0.002,
    )

    for pair in result.pairs:
        print(pair.first.index, pair.second.index, pair.delay)

Anchors are pairs of timestamps known to refer to the same event.  A
least-squares affine clock model is fitted to them, and ``window`` bounds the
residual propagation delay after that correction.  A scalar window ``w``
means ``[-w, +w]``; an explicit ``(earliest, latest)`` pair may be used for an
asymmetric window.

Payloads are compared only where both observations actually captured bytes.
Each observation keeps its own ``captured_range``, so a shorter snapshot does
not shrink the other side's record.  Identical payloads are never treated as
duplicates: they remain separate observations and separate possible
transmissions.

``result.pairs`` contains only uniquely determined one-to-one
correspondences.  If several maximum one-to-one assignments remain possible,
the whole candidate component is returned in ``result.candidate_groups``
instead of being resolved by file order; every edge in a group can occur in a
maximum matching.
